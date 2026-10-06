"""Validate economics pipeline on FREE_PACKAGE_FOUND BidNet packages (no discovery).

Evidence-only: never invent retail/gov value. Report exact break points.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from bidnet_recovery.states import FREE_PACKAGE_FOUND
from line_item_economics.engine import analyze_line_item_economics, load_analysis
from line_item_economics.extract import extract_line_items
from line_item_economics.identity import classify_all
from phase_l.l23_full_population_funnel import load_store, save_store
from profit_first.router import evaluate_opportunity_profit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "FREE_PACKAGE_ECONOMICS_VALIDATION_REPORT.json"

# Document relevance heuristics (bid package vs junk attach)
_BID_PKG = re.compile(
    r"\b(bid\s+document|invitation\s+to\s+bid|solicitation|specifications?|"
    r"bid\s+schedule|pricing\s+sheet|line\s+item|bill\s+of\s+materials|"
    r"scope\s+of\s+work|quantity|unit\s+price|proposal\s+form)\b",
    re.I,
)
_JUNK_DOC = re.compile(
    r"\b(form\s*w-?9|w-9|strategic\s+plan|terms\s+and\s+conditions|"
    r"sell\s+sheet|procurement\s+services|privacy\s+policy)\b",
    re.I,
)


def _pdf_text(path: str, max_pages: int = 60) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(path)
    except Exception:
        return ""
    parts = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        parts.append(page.get_text("text") or "")
    return "\n".join(parts)


def _doc_quality(name: str, text: str) -> str:
    blob = f"{name}\n{text[:8000]}"
    if _JUNK_DOC.search(blob) and not _BID_PKG.search(blob):
        return "NON_PACKAGE_DOCUMENT"
    if _BID_PKG.search(blob):
        return "LIKELY_BID_PACKAGE"
    if len(text.strip()) < 200:
        return "EMPTY_OR_UNREADABLE"
    return "AMBIGUOUS_DOCUMENT"


def _identity_stats(lines: list[dict[str, Any]]) -> dict[str, Any]:
    idc = Counter(str(l.get("identity_class") or "UNKNOWN") for l in lines)
    exact = sum(
        1
        for l in lines
        if l.get("nsn")
        or (l.get("part_number") and (l.get("manufacturer") or l.get("brand")))
        or (l.get("model") and (l.get("manufacturer") or l.get("brand")))
    )
    partial = sum(
        1
        for l in lines
        if l.get("manufacturer") or l.get("brand") or l.get("model") or l.get("part_number")
    )
    with_desc = sum(1 for l in lines if (l.get("product_description") or "").strip())
    with_qty = sum(1 for l in lines if l.get("quantity") is not None)
    samples = []
    for l in lines:
        if not (l.get("part_number") or l.get("model") or l.get("nsn") or l.get("manufacturer")):
            continue
        samples.append(
            {
                "desc": (l.get("product_description") or "")[:100],
                "mfr": l.get("manufacturer") or l.get("brand"),
                "model": l.get("model"),
                "pn": l.get("part_number"),
                "nsn": l.get("nsn"),
                "qty": l.get("quantity"),
                "uom": l.get("unit_of_measure"),
                "identity": l.get("identity_class"),
            }
        )
        if len(samples) >= 8:
            break
    return {
        "lines_total": len(lines),
        "lines_with_description": with_desc,
        "lines_with_qty": with_qty,
        "products_identified_exactish": exact,
        "products_with_any_identity_field": partial,
        "identity_classes": dict(idc.most_common()),
        "identity_samples": samples,
    }


def _try_gov_value(rec: dict[str, Any], lines: list[dict[str, Any]], text: str) -> dict[str, Any]:
    """Search only existing structured/history hooks + package text. No invented awards."""
    out: dict[str, Any] = {
        "found": False,
        "value": None,
        "basis": None,
        "attempts": [],
        "failure_reason": None,
    }
    # Stated estimate / budget in package text
    m = re.search(
        r"\b(?:estimated?\s+(?:value|cost|budget|amount)|budget(?:ary)?\s+amount|"
        r"engineer's?\s+estimate|not\s+to\s+exceed)\s*[:=]?\s*\$?\s*([\d,]+\.?\d*)\b",
        text or "",
        re.I,
    )
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            if val > 0:
                out.update(
                    {
                        "found": True,
                        "value": val,
                        "basis": "stated_estimate_in_package",
                        "evidence": m.group(0)[:160],
                    }
                )
                out["attempts"].append({"via": "package_estimate_regex", "ok": True})
                return out
        except ValueError:
            pass
    out["attempts"].append({"via": "package_estimate_regex", "ok": False})

    # Prior fields on record
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    for key, basis in (
        ("historical_award_price", "row_ref.historical_award_price"),
        ("historical_award_unit_price", "row_ref.historical_award_unit_price"),
    ):
        raw = rr.get(key)
        try:
            if raw is not None and float(raw) > 0:
                out.update({"found": True, "value": float(raw), "basis": basis})
                out["attempts"].append({"via": basis, "ok": True})
                return out
        except (TypeError, ValueError):
            pass
        out["attempts"].append({"via": basis, "ok": False})

    # USASpending buyer search (public API) — only if clear non-state buyer entity
    buyer = str(rec.get("buyer") or "")
    overview = str(rec.get("description") or "")
    entity = None
    em = re.search(
        r"\b([A-Z][A-Za-z0-9 .'-]{3,60}(?:Public Utility District|School District|County|City))\b",
        overview,
    )
    if em:
        entity = em.group(1)
    if entity and len(entity) > 8:
        try:
            import httpx

            payload = {
                "filters": {
                    "recipient_search_text": [entity[:80]],
                    "award_type_codes": ["A", "B", "C", "D"],
                },
                "fields": [
                    "Award ID",
                    "Recipient Name",
                    "Award Amount",
                    "Description",
                    "Start Date",
                ],
                "page": 1,
                "limit": 5,
                "sort": "Award Amount",
                "order": "desc",
            }
            r = httpx.post(
                "https://api.usaspending.gov/api/v2/search/spending_by_award/",
                json=payload,
                timeout=30.0,
            )
            out["attempts"].append(
                {
                    "via": "usaspending_recipient_search",
                    "entity": entity,
                    "http": r.status_code,
                }
            )
            if r.status_code == 200:
                rows = (r.json() or {}).get("results") or []
                # Do NOT auto-accept as this opportunity's gov value — only note availability
                out["usaspending_hits"] = len(rows)
                if rows:
                    out["usaspending_sample"] = [
                        {
                            "award_id": row.get("Award ID"),
                            "amount": row.get("Award Amount"),
                            "desc": str(row.get("Description") or "")[:120],
                        }
                        for row in rows[:3]
                    ]
                    out["failure_reason"] = (
                        "USASpending hits exist for buyer entity but no award "
                        "tied to this solicitation — not attributing as gov value"
                    )
                    return out
        except Exception as exc:
            out["attempts"].append(
                {"via": "usaspending_recipient_search", "ok": False, "error": type(exc).__name__}
            )
    else:
        out["attempts"].append(
            {"via": "usaspending_recipient_search", "ok": False, "error": "NO_CLEAR_BUYER_ENTITY"}
        )

    out["failure_reason"] = out.get("failure_reason") or (
        "No stated estimate in package; no historical award on record; "
        "no solicitation-linked public award tab"
    )
    return out


def _parse_proposal_schedule(text: str) -> list[dict[str, Any]]:
    """Parse TDPUD-style proposal schedule rows (often one field per line in PDF text)."""
    rows: list[dict[str, Any]] = []
    if not text or "Proposal Schedule" not in text and "Item #" not in text:
        # Still try inventory-pattern parse below
        pass

    # Collapse to tokens by line for multiline tables
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    i = 0
    while i < len(lines) - 4:
        item = lines[i]
        inv = lines[i + 1]
        if not re.fullmatch(r"\d{1,3}", item):
            i += 1
            continue
        if not re.fullmatch(r"\d{5,12}|NON-INV", inv, re.I):
            i += 1
            continue
        # description may span 1-3 lines until qty
        desc_parts = []
        j = i + 2
        while j < len(lines) and not re.fullmatch(r"\d{1,5}", lines[j]) and len(desc_parts) < 4:
            if re.search(r"Unit Price|Total Price|Proposal Schedule|TOTAL AMOUNT", lines[j], re.I):
                break
            desc_parts.append(lines[j])
            j += 1
        if j >= len(lines) or not re.fullmatch(r"\d{1,5}", lines[j]):
            i += 1
            continue
        qty = lines[j]
        uom = lines[j + 1] if j + 1 < len(lines) else ""
        if not re.search(r"EA|FEET|FT|EACH|GAL|LB|SET", uom, re.I):
            i += 1
            continue
        desc = " ".join(desc_parts).strip(" *")
        if item == "0" or "EXAMPLE" in desc.upper():
            i = j + 2
            continue
        mfr = None
        model = None
        pn = None
        if re.search(r"\bFORD\b", desc, re.I):
            mfr = "Ford"
            mm = re.search(r"\b(Y\d+[A-Z0-9\-]*)\b", desc, re.I)
            if mm:
                model = mm.group(1)
            mm = re.search(r"\b(C[\dA-Z\-]+(?:-NL)?)\b", desc, re.I)
            if mm:
                pn = mm.group(1)
        if re.search(r"\bSMITH-?BLAIR\b", desc, re.I):
            mfr = "Smith-Blair"
            mm = re.search(r"\b(\d{3}-\d{6,12}-\d{3})\b", desc)
            if mm:
                pn = mm.group(1)
        uom_n = re.sub(r"\s*[-–].*$", "", uom).strip().upper()
        if uom_n.startswith("EA"):
            uom_n = "EA"
        elif "FEET" in uom_n or uom_n.startswith("FT"):
            uom_n = "FT"
        rows.append(
            {
                "clin": item,
                # Buyer inventory # is NOT a commercial part number — do not put in sku/pn
                "description": desc,
                "quantity": float(qty),
                "uom": uom_n,
                "manufacturer": mfr,
                "model": model,
                "part_number": pn,
                "inventory_number": inv if inv.upper() != "NON-INV" else None,
            }
        )
        i = j + 2
    return rows


def _try_acquisition_cost(
    rec: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    max_lookups: int = 5,
) -> dict[str, Any]:
    """Attempt public retail for exact-identity lines only. Evidence-gated."""
    out: dict[str, Any] = {
        "found": False,
        "priced_lines": 0,
        "attempts": [],
        "failure_reason": None,
        "retail_by_line": {},
    }
    candidates = []
    for l in lines:
        pn = l.get("part_number") or l.get("model")
        mfr = l.get("manufacturer") or l.get("brand")
        nsn = l.get("nsn")
        desc = (l.get("product_description") or "").strip()
        if nsn or (mfr and (pn or l.get("model"))) or (pn and mfr):
            candidates.append(l)
        elif mfr and desc and len(desc) > 12:
            candidates.append(l)
    if not candidates:
        out["failure_reason"] = "No exact product identity (mfr/model/PN/NSN) to look up retail"
        out["attempts"].append({"via": "identity_gate", "ok": False})
        return out

    try:
        from phase_l.acquisition_pricing import DomainCircuitBreaker, research_acquisition_price
    except Exception as exc:
        out["failure_reason"] = f"retail_resolver_unavailable:{type(exc).__name__}"
        return out

    priced = 0
    breaker = DomainCircuitBreaker(fail_threshold=3)
    for l in candidates[:max_lookups]:
        lid = str(l.get("line_id") or l.get("clin") or l.get("line_number") or "")
        commercial = {
            "manufacturer": l.get("manufacturer") or l.get("brand"),
            "model": l.get("model"),
            "part_number": l.get("part_number"),
            "mpn": l.get("part_number") or l.get("model"),
            "sku": l.get("sku"),
            "nsn": l.get("nsn"),
            "commercial_identity_state": "EXACT" if (l.get("part_number") or l.get("model")) else "PARTIAL",
        }
        row = {
            "title": l.get("product_description") or rec.get("title"),
            "description": l.get("product_description"),
        }
        query = " ".join(
            str(x)
            for x in (
                commercial.get("manufacturer"),
                commercial.get("model"),
                commercial.get("part_number"),
                row.get("description"),
            )
            if x
        ).strip()
        try:
            result = research_acquisition_price(
                row=row,
                commercial=commercial,
                identity=commercial,
                breaker=breaker,
                learning={},
                memory={},
                max_fetches=4,
                allow_bing_fallback=False,
            )
        except Exception as exc:
            out["attempts"].append(
                {"line": lid, "query": query[:80], "ok": False, "error": type(exc).__name__}
            )
            continue

        usable = list((result or {}).get("usable") or [])
        verified = list((result or {}).get("verified") or [])
        pick = None
        for v in usable or verified:
            if not isinstance(v, dict):
                continue
            p = v.get("verified_price") or v.get("unit_price")
            if p is None:
                continue
            if usable and v not in usable and not v.get("economics_eligible"):
                continue
            pick = v
            break
        ok = bool(pick)
        price = (pick or {}).get("verified_price") or (pick or {}).get("unit_price")
        url = (pick or {}).get("source_url") or (result or {}).get("source_url")
        out["attempts"].append(
            {
                "line": lid,
                "query": query[:80],
                "ok": ok,
                "price": price,
                "confidence": "VERIFIED" if ok else (result or {}).get("failure_class"),
                "url": (str(url)[:160] if url else None),
                "usable_n": len(usable),
                "verified_n": len(verified),
                "failure_class": (result or {}).get("failure_class"),
            }
        )
        if ok and price is not None:
            priced += 1
            out["retail_by_line"][lid or query[:40]] = {
                "unit_price": float(price),
                "source_url": url,
                "confidence": "VERIFIED",
                "kind": "public_retail",
            }

    out["priced_lines"] = priced
    out["found"] = priced > 0
    if not out["found"]:
        out["failure_reason"] = (
            f"Tried {min(len(candidates), max_lookups)} identity-bearing lines; "
            "no verified/usable public retail accepted (fail-closed)"
        )
    return out


def _inspect_signature(fn) -> str:
    import inspect

    try:
        return str(inspect.signature(fn))
    except Exception:
        return "?"


def validate_one(cid: str, rec: dict[str, Any]) -> dict[str, Any]:
    chase = (rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}
    free_docs = [
        d
        for d in (rec.get("attachments_metadata") or [])
        if isinstance(d, dict) and d.get("free_chase")
    ]
    doc_reports = []
    texts = []
    for d in free_docs:
        path = d.get("local_path")
        name = str(d.get("document_name") or "")
        text = _pdf_text(path) if path and Path(str(path)).exists() else ""
        if not text and d.get("document_url"):
            # already local preferred
            pass
        q = _doc_quality(name, text)
        doc_reports.append(
            {
                "name": name[:120],
                "local_path": path,
                "bytes": d.get("byte_size"),
                "quality": q,
                "text_chars": len(text),
                "url": str(d.get("document_url") or "")[:160],
            }
        )
        if q == "LIKELY_BID_PACKAGE" and text:
            texts.append(text)
        elif q == "AMBIGUOUS_DOCUMENT" and text and len(text) > 1000:
            texts.append(text)

    package_text = "\n\n".join(texts)
    useful_docs = sum(1 for d in doc_reports if d["quality"] == "LIKELY_BID_PACKAGE")

    # Extract lines from useful package text only (not junk PDFs)
    schedule_rows = _parse_proposal_schedule(package_text) if package_text else []
    if schedule_rows:
        extracted = extract_line_items(schedule_rows=schedule_rows)
    else:
        extracted = extract_line_items(body_text=package_text[:400000] if package_text else "")
    lines = extracted.get("lines") or []
    # Drop empty UNKNOWN junk from prior bad CSV parses if reusing
    lines = [
        l
        for l in lines
        if (l.get("product_description") or "").strip()
        or l.get("part_number")
        or l.get("model")
        or l.get("nsn")
    ]
    classify_all(lines)
    analysis = analyze_line_item_economics(
        opportunity_id=cid,
        title=rec.get("title"),
        buyer=rec.get("buyer"),
        body_text=package_text[:400000] if package_text else str(rec.get("title") or ""),
        schedule_rows=schedule_rows or None,
        existing_lines=lines if lines and not schedule_rows else None,
        persist=True,
    )
    lines = analysis.get("lines") or lines
    lines = [
        l
        for l in lines
        if (l.get("product_description") or "").strip()
        or l.get("part_number")
        or l.get("model")
        or l.get("nsn")
    ]
    id_stats = _identity_stats(lines)

    gov = _try_gov_value(rec, lines, package_text)
    acq = _try_acquisition_cost(rec, lines, max_lookups=5)

    # Re-run economics with any verified retail attached (gov only if found for THIS opp)
    retail_by_line = acq.get("retail_by_line") or {}
    historical_by_line = {}
    # Do not attach USASpending samples as line historical — not solicitation-linked

    analysis2 = analyze_line_item_economics(
        opportunity_id=cid,
        title=rec.get("title"),
        buyer=rec.get("buyer"),
        body_text=package_text[:400000] if package_text else str(rec.get("title") or ""),
        schedule_rows=schedule_rows or None,
        existing_lines=lines if lines and not schedule_rows else None,
        retail_by_line=retail_by_line or None,
        historical_by_line=historical_by_line or None,
        persist=True,
    )

    pev = evaluate_opportunity_profit(
        opportunity_id=cid,
        rec=rec,
        title=rec.get("title"),
        buyer=rec.get("buyer"),
        line_item_analysis=analysis2,
        expected_revenue=gov.get("value") if gov.get("found") else None,
        ranking_signals={
            "exact_identity": id_stats["products_identified_exactish"] > 0,
            "public_retail_available": bool(acq.get("found")),
            "free_package": True,
        },
    )
    econ = pev.get("economics") or {}
    roll = analysis2.get("rollup") or {}

    gov_found = bool(gov.get("found") or roll.get("TOTAL_KNOWN_HISTORICAL_VALUE"))
    cost_found = bool(acq.get("found") or roll.get("TOTAL_KNOWN_RETAIL_COST"))
    both = bool(gov_found and cost_found)

    # Determine primary failure reason (first break)
    if useful_docs == 0:
        failure = "PACKAGE_DOCS_NOT_ACTUAL_BID_PACKAGE"
    elif id_stats["lines_with_description"] == 0:
        failure = "LINE_ITEM_EXTRACTION_FAILED"
    elif id_stats["products_identified_exactish"] == 0 and id_stats["products_with_any_identity_field"] == 0:
        failure = "PRODUCT_IDENTITY_MISSING"
    elif not gov_found and not cost_found:
        failure = "BOTH_SIDES_MISSING"
    elif not gov_found:
        failure = "GOVERNMENT_VALUE_MISSING"
    elif not cost_found:
        failure = "ACQUISITION_COST_MISSING"
    elif econ.get("expected_profit") is None:
        failure = "PROFIT_NOT_COMPUTABLE"
    else:
        failure = None

    # Persist validation block on record
    rec.setdefault("bidnet_recovery", {})
    if isinstance(rec["bidnet_recovery"], dict):
        rec["bidnet_recovery"]["economics_validation"] = {
            "validated_at": now_utc().isoformat(),
            "failure_reason": failure,
            "gov_found": gov_found,
            "cost_found": cost_found,
            "both_sides": both,
            "lines": id_stats["lines_total"],
            "exact_identity": id_stats["products_identified_exactish"],
            "profit_status": econ.get("profit_status"),
            "expected_profit": econ.get("expected_profit"),
        }
    rec["profit_first"] = {
        "profit_status": econ.get("profit_status"),
        "expected_profit": econ.get("expected_profit"),
        "post_financing_profit": econ.get("post_financing_profit"),
        "route": pev.get("route"),
        "research_priority": (pev.get("research") or {}).get("priority"),
        "missing_facts": econ.get("missing_facts"),
        "evaluated_at": pev.get("evaluated_at"),
        "validation_run": True,
    }
    pe = rec.setdefault("product_economics", {})
    if isinstance(pe, dict):
        pe["both_sides_known"] = both
        if gov_found:
            pe["gov_value"] = gov.get("value") or roll.get("TOTAL_KNOWN_HISTORICAL_VALUE")
        if cost_found:
            pe["acquisition_cost"] = roll.get("TOTAL_KNOWN_RETAIL_COST")

    return {
        "canonical_id": cid,
        "package": str(rec.get("title") or "")[:120],
        "buyer": rec.get("buyer"),
        "recovery_route": chase.get("recovery_route"),
        "documents": doc_reports,
        "useful_bid_package_docs": useful_docs,
        "lines_extracted": id_stats["lines_total"],
        "lines_with_description": id_stats["lines_with_description"],
        "products_identified": id_stats["products_identified_exactish"],
        "products_with_any_identity_field": id_stats["products_with_any_identity_field"],
        "identity_classes": id_stats["identity_classes"],
        "identity_samples": id_stats["identity_samples"],
        "government_value_found": gov_found,
        "government_value": gov.get("value"),
        "government_value_basis": gov.get("basis"),
        "government_value_detail": {
            "attempts": gov.get("attempts"),
            "failure_reason": gov.get("failure_reason"),
            "usaspending_hits": gov.get("usaspending_hits"),
        },
        "acquisition_cost_found": cost_found,
        "acquisition_cost_priced_lines": acq.get("priced_lines"),
        "acquisition_cost_detail": {
            "attempts": acq.get("attempts"),
            "failure_reason": acq.get("failure_reason"),
        },
        "both_sides_known": both,
        "freight_known": bool(roll.get("freight_known")),
        "freight": roll.get("estimated_freight"),
        "financing_cost": roll.get("financing_cost"),
        "expected_profit": econ.get("expected_profit"),
        "profit_status": econ.get("profit_status"),
        "confidence": {
            "completeness_grade": roll.get("completeness_grade"),
            "coverage_pct": roll.get("coverage_pct"),
            "evidence_grade": econ.get("evidence_grade"),
            "proof_label": roll.get("proof_label"),
        },
        "research_priority": (pev.get("research") or {}).get("priority"),
        "missing_facts": econ.get("missing_facts"),
        "failure_reason": failure,
        "extraction_source": (analysis2.get("extraction") or {}).get("source_used"),
    }


def main() -> int:
    store = load_store()
    targets = []
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        ch = ((rec.get("bidnet_recovery") or {}).get("free_package_chase") or {})
        if ch.get("status") == FREE_PACKAGE_FOUND:
            targets.append((cid, rec))
    targets.sort(key=lambda x: x[0])

    print(f"validating {len(targets)} FREE_PACKAGE_FOUND packages")
    # Show retail resolver signature once
    try:
        from phase_l.product_page_resolution import resolve_and_verify_market_price as _r

        print("resolve_and_verify_market_price", _inspect_signature(_r))
    except Exception as exc:
        print("resolver_import", type(exc).__name__)

    rows = []
    for cid, rec in targets:
        print("→", cid[:12], str(rec.get("title") or "")[:70], flush=True)
        row = validate_one(cid, rec)
        store[cid] = rec
        rows.append(row)
        print(
            "  lines",
            row["lines_extracted"],
            "ident",
            row["products_identified"],
            "gov",
            row["government_value_found"],
            "cost",
            row["acquisition_cost_found"],
            "fail",
            row["failure_reason"],
            flush=True,
        )

    save_store(store)
    report = {
        "kind": "FreePackageEconomicsValidationReport",
        "generated_at": now_utc().isoformat(),
        "packages_validated": len(rows),
        "summary": {
            "with_useful_bid_docs": sum(1 for r in rows if r["useful_bid_package_docs"] > 0),
            "with_lines": sum(1 for r in rows if r["lines_with_description"] > 0),
            "with_product_identity": sum(1 for r in rows if r["products_identified"] > 0),
            "gov_value_found": sum(1 for r in rows if r["government_value_found"]),
            "acquisition_cost_found": sum(1 for r in rows if r["acquisition_cost_found"]),
            "both_sides_known": sum(1 for r in rows if r["both_sides_known"]),
            "profitable": sum(
                1
                for r in rows
                if r.get("expected_profit") is not None and float(r["expected_profit"]) > 0
            ),
            "failure_reasons": dict(Counter(r["failure_reason"] or "NONE" for r in rows)),
        },
        "packages": rows,
        "note": (
            "Evidence-only validation. No discovery. No invented prices. "
            "Purpose: locate post-document economics break points."
        ),
    }
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", OUT)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
