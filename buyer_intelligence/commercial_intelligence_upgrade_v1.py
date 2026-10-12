"""Commercial Intelligence & Profitability Upgrade V1.

Post-HT-v2 layer: diagnose unmatched suppliers, rematch with repaired catalogs,
recover public retail / historical evidence on a bounded pilot, compute
Supplier Advantage + max allowable supplier cost, multi-line completeness,
financing labels, packet reconciliation, and Monday outreach RFQ packets.

Does NOT modify HT v2 engine behavior. Does NOT fabricate prices. Does NOT send
external supplier messages. SAM API: prefer 0 calls (uses cache only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.multi_supplier_basket_line_matching_v1 import (
    FAMILY_SUPPLIERS,
    SUPPORTED_FITS,
    best_supplier_for_line,
    classify_family,
    fit_to_mission,
)
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    assess_line_fit,
    resolve_contact,
)

log = logging.getLogger("govtracker.commercial_intelligence_upgrade_v1")

BUILD = "20261012-m3-commercial-intelligence-upgrade-v1"
FAMILY = "commercial_intelligence_upgrade_v1"

HT_RESULTS = "m3_ht_max_evidence_v2_results.json"
HT_PACKETS = "m3_ht_max_evidence_v2_packets.json"
AUDIT_JSON = "_og_commercial_research_pipeline_out/m3_commercial_viability_audit_htv2.json"

OUT_DIR = "_og_commercial_research_pipeline_out"
REPORT_FILE = f"{OUT_DIR}/m3_commercial_intelligence_upgrade_v1_report.json"
DIAG_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_match_diagnosis.json"
REMATCH_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_rematch.json"
PILOT_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_pilot_research.json"
OUTREACH_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_monday_outreach.json"
PACKET_RECON_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_packet_reconciliation.json"
DEALS_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_available_deals.json"
CHECKPOINT_FILE = f"{OUT_DIR}/m3_ci_upgrade_v1_checkpoint.json"

FAILURE_CODES = (
    "BAD_PRODUCT_EXTRACTION",
    "MISSING_MODEL_OR_MPN",
    "INSUFFICIENT_SEARCH_VARIANTS",
    "CATEGORY_MISCLASSIFIED",
    "SUPPLIER_CATALOG_COVERAGE",
    "SUPPLIER_RESTRICTED",
    "GENUINELY_SPECIALTY",
    "INSUFFICIENT_SPECIFICATIONS",
    "EXTERNAL_SOURCE_UNAVAILABLE",
    "OTHER_VERIFIED_REASON",
)

FIN_CONFIRMED = "FINANCEABLE_CONFIRMED"
FIN_UNVERIFIED = "FINANCING_PATH_UNVERIFIED"
FIN_BLOCKED = "FINANCING_BLOCKED"
FIN_UNKNOWN = "FINANCING_UNKNOWN"

PRICE_LEVELS = (
    "EXACT_PUBLIC_PRICE_VERIFIED",
    "EQUIVALENT_PUBLIC_PRICE_VERIFIED",
    "DISTRIBUTOR_PRICE_VERIFIED",
    "ACTUAL_SUPPLIER_QUOTE",
    "HISTORICAL_INTERNAL_PURCHASE_COST",
    "PRICE_UNAVAILABLE",
    "PRICE_IDENTITY_UNCERTAIN",
)

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_MPN_RE = re.compile(r"\b(?:p/?n|mpn|model|part(?:\s*(?:no|number|#))?)\s*[:#]?\s*([A-Z0-9][\w\-/]{2,})\b", re.I)
_QTY_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(ea|each|cs|case|bx|box|pk|pack|kit|set|ft|feet|gal|lb|lbs)?\b", re.I)

MONEY = Decimal("0.01")


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("load failed %s: %s", name, exc)
        return None


def _save(name: str, obj: Any) -> Path:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return p


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL", "N/A"}:
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip()).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money_str(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def load_ht_rows() -> list[dict[str, Any]]:
    raw = _load(HT_RESULTS) or {}
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    return [r for r in (rows or []) if isinstance(r, dict)]


def quoteable_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = rows if rows is not None else load_ht_rows()
    return [r for r in rows if int(r.get("QUOTEABLE_LINES") or 0) > 0]


def unmatched_quoteable(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    q = quoteable_rows(rows)
    return [
        r
        for r in q
        if int(r.get("SUPPORTED_SUPPLIER_LINES") or 0) == 0 and str(r.get("PRODUCT_FIT") or "") != "EXCLUDED"
    ]


# ---------------------------------------------------------------------------
# Product identity / query expansion
# ---------------------------------------------------------------------------


def normalize_product_identity(text: str, *, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    structured = structured or {}
    t = (text or "").strip()
    mpn = (
        structured.get("MPN")
        or structured.get("mpn")
        or structured.get("MODEL")
        or structured.get("model")
        or structured.get("CATALOG")
        or structured.get("catalog")
    )
    if not mpn:
        m = _MPN_RE.search(t)
        mpn = m.group(1) if m else None
    nsn = structured.get("NSN") or structured.get("nsn")
    if not nsn:
        m = _NSN_RE.search(t)
        nsn = m.group(0) if m else None
    mfr = structured.get("MANUFACTURER") or structured.get("manufacturer") or structured.get("BRAND") or structured.get("brand")
    qty = structured.get("QTY") if structured.get("QTY") is not None else structured.get("qty")
    uom = structured.get("UOM") or structured.get("uom")
    if qty is None:
        qm = _QTY_RE.search(t)
        if qm:
            try:
                qty = float(qm.group(1))
            except ValueError:
                qty = None
            uom = uom or (qm.group(2) or "EA")
    # Alternate MPN formats
    alts: list[str] = []
    if mpn:
        alts.append(str(mpn))
        alts.append(str(mpn).replace("-", ""))
        alts.append(str(mpn).replace(" ", ""))
        alts.append(str(mpn).upper())
        alts.append(str(mpn).lower())
    synonyms = _synonyms_for(t)
    return {
        "raw_description": t,
        "description": t,
        "manufacturer": mfr,
        "brand": mfr,
        "part_number": mpn,
        "model": mpn,
        "nsn": nsn,
        "qty": qty,
        "uom": (str(uom).upper() if uom else None),
        "mpn_variants": sorted(set(a for a in alts if a)),
        "synonyms": synonyms,
        "has_exact_id": bool(mpn or nsn),
    }


def _synonyms_for(text: str) -> list[str]:
    t = text.lower()
    out: list[str] = []
    pairs = [
        (("paper towel", "paper towels"), ["paper towels", "hardwound paper towels"]),
        (("toilet paper", "bathroom tissue"), ["toilet paper", "bath tissue"]),
        (("janitorial",), ["janitorial supplies", "paper towels", "cleaning supplies"]),
        (("mop hardware", "mop"), ["mop handle", "mop head", "wet mop hardware"]),
        (("ppe", "safety products", "safety supplies"), ["nitrile gloves", "safety glasses", "hard hat"]),
        (("led", "lighting", "luminaire"), ["led light fixture", "led bulb"]),
        (("filter element",), ["hydraulic filter element", "fluid filter"]),
        (("brake", "caliper", "rotor"), ["brake rotor", "brake caliper"]),
        (("toner",), ["toner cartridge"]),
        (("electrical parts", "electrical supplies"), ["electrical supplies", "circuit breaker"]),
        (("gasket",), ["gasket material", "flange gasket"]),
        (("valve,check", "check valve", "valve, check"), ["check valve"]),
        (("safety relief", "valve,safety"), ["safety relief valve"]),
    ]
    for keys, syns in pairs:
        if any(k in t for k in keys):
            out.extend(syns)
    if not any(k in t for k in ("brake", "caliper", "rotor")) and re.search(
        r"\b(fastener|hardware|bolt|nut|screw)\b", t
    ):
        out.extend(["hex bolts", "fasteners"])
    return out


def expand_search_queries(identity: dict[str, Any]) -> list[str]:
    """Expand queries beyond public_price_search baseline (UOM/pack + synonyms)."""
    from public_price_search.queries import build_query_variants

    base = build_query_variants(identity)
    extra: list[str] = []
    for v in identity.get("mpn_variants") or []:
        extra.extend([v, f"{v} price", f"{v} buy"])
    mfr = identity.get("manufacturer") or identity.get("brand")
    for syn in identity.get("synonyms") or []:
        extra.append(syn)
        if mfr:
            extra.append(f"{mfr} {syn}")
    uom = identity.get("uom")
    pn = identity.get("part_number")
    if pn and uom:
        extra.append(f"{pn} {uom}")
        extra.append(f"{pn} per {uom}")
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in list(base) + extra:
        qn = re.sub(r"\s+", " ", str(q or "").strip())
        if len(qn) < 3 or qn.lower() in seen:
            continue
        seen.add(qn.lower())
        out.append(qn)
    return out


# ---------------------------------------------------------------------------
# Phase 1 — diagnosis + rematch
# ---------------------------------------------------------------------------


def _proxy_line_from_row(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("TITLE") or "").strip()
    return {
        "DESCRIPTION": title,
        "description": title,
        "PROVENANCE": "TITLE_PROXY_FOR_REMATCH",
        "QTY": None,
        "UOM": None,
    }


def diagnose_unmatched_sample(
    unmatched: list[dict[str, Any]],
    *,
    sample_size: int = 120,
) -> dict[str, Any]:
    """Stratified sample diagnosis with explicit failure codes (no fabricated matches)."""
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in unmatched:
        by_src[str(r.get("SOURCE") or "UNKNOWN")].append(r)
    sample: list[dict[str, Any]] = []
    # Round-robin across sources for stratification
    buckets = {k: list(v) for k, v in by_src.items()}
    while len(sample) < sample_size and any(buckets.values()):
        for k in list(buckets.keys()):
            if not buckets[k]:
                continue
            sample.append(buckets[k].pop(0))
            if len(sample) >= sample_size:
                break
    # Fill from remaining by real_lines diversity
    if len(sample) < sample_size:
        remain = [r for r in unmatched if r not in sample]
        remain.sort(key=lambda r: (-int(r.get("REAL_LINES") or 0), r.get("OPPORTUNITY_ID") or ""))
        sample.extend(remain[: sample_size - len(sample)])

    diagnoses: list[dict[str, Any]] = []
    code_counts: Counter = Counter()
    for row in sample:
        oid = str(row.get("OPPORTUNITY_ID") or "")
        title = str(row.get("TITLE") or "")
        real = int(row.get("REAL_LINES") or 0)
        qlines = int(row.get("QUOTEABLE_LINES") or 0)
        fit_counts = row.get("SUPPLIER_FIT_COUNTS") or {}
        modes = row.get("LINE_QUOTE_MODES") or {}
        identity = normalize_product_identity(title)
        fam = classify_family({}, title)
        match = best_supplier_for_line(_proxy_line_from_row(row), fam, None)
        codes: list[str] = []
        commercially_available: str = "UNKNOWN"

        if real <= 0 and qlines > 0:
            codes.append("BAD_PRODUCT_EXTRACTION")
        if not identity.get("has_exact_id"):
            codes.append("MISSING_MODEL_OR_MPN")
        if int(fit_counts.get("NO_FIT") or 0) > 0 and fam == "other":
            codes.append("CATEGORY_MISCLASSIFIED")
        if int(fit_counts.get("NO_FIT") or 0) > 0 and fam != "other" and not match.get("SUPPORTED"):
            codes.append("SUPPLIER_CATALOG_COVERAGE")
        if int(fit_counts.get("PARTIAL_MATCH") or 0) > 0 and not match.get("SUPPORTED"):
            # Prior path treated commodity PARTIAL as unsupported — catalog/evidence gap
            codes.append("INSUFFICIENT_SPECIFICATIONS")
        if fam == "nsn_defense" or _NSN_RE.search(title):
            codes.append("SUPPLIER_RESTRICTED")
            commercially_available = "RESTRICTED_OR_APPROVED_SOURCE"
        if re.search(r"\b(design[- ]build|contractor services|installation|construction)\b", title, re.I):
            codes.append("GENUINELY_SPECIALTY")
            commercially_available = "LIKELY_SERVICE_OR_SPECIALTY"
        if match.get("SUPPORTED"):
            commercially_available = "LIKELY_COMMERCIAL"
            if "SUPPLIER_CATALOG_COVERAGE" in codes:
                codes.remove("SUPPLIER_CATALOG_COVERAGE")
            codes.append("SUPPLIER_CATALOG_COVERAGE")  # was software-repairable
            # Tag as repaired-at-diagnosis (software cause)
            codes = [c if c != "SUPPLIER_CATALOG_COVERAGE" else "SUPPLIER_CATALOG_COVERAGE" for c in codes]
            codes.append("OTHER_VERIFIED_REASON")
        if not codes:
            codes.append("OTHER_VERIFIED_REASON")

        # Primary code = first most actionable
        primary = codes[0]
        for preferred in (
            "SUPPLIER_RESTRICTED",
            "GENUINELY_SPECIALTY",
            "BAD_PRODUCT_EXTRACTION",
            "CATEGORY_MISCLASSIFIED",
            "SUPPLIER_CATALOG_COVERAGE",
            "INSUFFICIENT_SPECIFICATIONS",
            "MISSING_MODEL_OR_MPN",
        ):
            if preferred in codes:
                primary = preferred
                break
        code_counts[primary] += 1
        diagnoses.append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": title[:160],
                "BUYER": row.get("BUYER"),
                "SOURCE": row.get("SOURCE"),
                "REAL_LINES": real,
                "QUOTEABLE_LINES": qlines,
                "PRIOR_FIT_COUNTS": fit_counts,
                "LINE_QUOTE_MODES": modes,
                "FAMILY_NOW": fam,
                "REMATCH_PREVIEW_SUPPORTED": bool(match.get("SUPPORTED")),
                "REMATCH_PREVIEW_SUPPLIER": match.get("SUPPLIER"),
                "FAILURE_CODES": codes,
                "PRIMARY_FAILURE_CODE": primary,
                "COMMERCIALLY_AVAILABLE": commercially_available,
                "IDENTITY": {
                    "has_exact_id": identity.get("has_exact_id"),
                    "part_number": identity.get("part_number"),
                    "nsn": identity.get("nsn"),
                    "synonyms": identity.get("synonyms"),
                },
            }
        )

    return {
        "kind": "ci_upgrade_match_diagnosis",
        "build": BUILD,
        "built_at": _utc(),
        "SAMPLE_SIZE": len(diagnoses),
        "UNMATCHED_POPULATION": len(unmatched),
        "PRIMARY_FAILURE_COUNTS": dict(code_counts),
        "STRATIFICATION_BY_SOURCE": {k: len(v) for k, v in by_src.items()},
        "DIAGNOSES": diagnoses,
    }


def rematch_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    """Rematch one quoteable opportunity using repaired catalogs + title/line proxy."""
    oid = str(row.get("OPPORTUNITY_ID") or "")
    title = str(row.get("TITLE") or "")
    prior_supported = int(row.get("SUPPORTED_SUPPLIER_LINES") or 0)
    prior_packets = int(row.get("PACKET_COUNT") or 0)
    # Prefer any embedded packets' line context; else title proxy
    lines = [_proxy_line_from_row(row)]
    # If multiple quoteable lines known, replicate proxy for coverage accounting (labeled)
    qlines = max(1, int(row.get("QUOTEABLE_LINES") or 1))
    if qlines > 1:
        lines = [{**_proxy_line_from_row(row), "LINE_ID": i, "PROXY_FOR_QUOTEABLE_LINE": True} for i in range(1, min(qlines, 40) + 1)]

    supported = 0
    supplier_set: dict[str, int] = defaultdict(int)
    line_results: list[dict[str, Any]] = []
    for ln in lines:
        blob = " ".join(str(ln.get(k) or "") for k in ("DESCRIPTION", "description", "MANUFACTURER", "MPN", "NSN"))
        fam = classify_family(ln, blob or title)
        match = best_supplier_for_line(ln, fam, None)
        # Also try alternate families from title keywords when primary fails
        if not match.get("SUPPORTED"):
            for alt_fam in ("mro", "janitorial", "tools", "ppe", "electrical", "plumbing"):
                if alt_fam == fam:
                    continue
                alt = best_supplier_for_line(ln, alt_fam, None)
                if alt.get("SUPPORTED"):
                    match = alt
                    fam = alt_fam
                    break
        if match.get("SUPPORTED") and match.get("SUPPLIER"):
            supported += 1
            supplier_set[str(match["SUPPLIER"])] += 1
        line_results.append({"FAMILY": fam, "MATCH": match})

    packets: list[dict[str, Any]] = []
    for sup, nlines in supplier_set.items():
        contact = resolve_contact(sup, (SUPPLIER_META.get(sup) or {}).get("KEY"))
        outreach = "READY_VIA_FORM" if contact.get("CONTACT_STATE") == "VERIFIED_FORM" else "RECIPIENT_RESEARCH"
        packets.append(
            {
                "PACKET_ID": f"CI-{oid[:10]}-{hashlib.sha1(sup.encode()).hexdigest()[:8]}",
                "OPPORTUNITY_ID": oid,
                "SUPPLIER": sup,
                "SUPPORTED_LINES": nlines,
                "CONTACT": contact,
                "OUTREACH_STATE": outreach,
                "CHANNEL_CLASS": (SUPPLIER_META.get(sup) or {}).get("CHANNEL_CLASS") or "GENERAL_COMMERCIAL",
                "AUTO_SEND": False,
                "SOURCE_LAYER": BUILD,
            }
        )

    return {
        "OPPORTUNITY_ID": oid,
        "TITLE": title,
        "BUYER": row.get("BUYER"),
        "DEADLINE": row.get("DEADLINE"),
        "SOURCE": row.get("SOURCE"),
        "PRODUCT_FIT": row.get("PRODUCT_FIT"),
        "PRIOR_SUPPORTED_SUPPLIER_LINES": prior_supported,
        "PRIOR_PACKET_COUNT": prior_packets,
        "QUOTEABLE_LINES": qlines,
        "REAL_LINES": int(row.get("REAL_LINES") or 0),
        "SUPPORTED_SUPPLIER_LINES_AFTER": supported,
        "SUPPLIER_SET_AFTER": dict(supplier_set),
        "NEW_MATCH": supported > 0 and prior_supported == 0,
        "PACKETS_AFTER": packets,
        "LINE_PROXY_USED": True,
        "LINE_RESULTS": line_results[:5],
        "MULTI_LINE": multi_line_completeness(
            total_required=qlines,
            lines_identified=qlines if int(row.get("REAL_LINES") or 0) > 0 else 0,
            lines_supplier_matched=supported,
            lines_price_verified=0,
            lines_financially_covered=0,
            award_structure=str(row.get("AWARD_STRUCTURE") or ""),
        ),
    }


def rematch_population(unmatched: list[dict[str, Any]], *, limit: int | None = None) -> dict[str, Any]:
    rows = unmatched[:limit] if limit else unmatched
    results: list[dict[str, Any]] = []
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        results.append(rematch_opportunity(row))
        if i % 50 == 0:
            log.info("rematch progress %s/%s", i, len(rows))
            _save(
                CHECKPOINT_FILE,
                {"stage": "rematch", "done": i, "total": len(rows), "at": _utc()},
            )
    new_matches = [r for r in results if r.get("NEW_MATCH")]
    packets = []
    for r in results:
        packets.extend(r.get("PACKETS_AFTER") or [])
    return {
        "kind": "ci_upgrade_rematch",
        "build": BUILD,
        "built_at": _utc(),
        "ORIGINAL_UNMATCHED": len(unmatched),
        "REPROCESSED": len(results),
        "NEW_LEGITIMATE_MATCHES": len(new_matches),
        "REMAINING_UNMATCHED": len(results) - len(new_matches),
        "NEW_PACKET_READY_OPPS": sum(1 for r in new_matches if r.get("PACKETS_AFTER")),
        "NEW_PACKETS": len(packets),
        "FALSE_POSITIVE_RATE_NOTE": (
            "Matches are channel/catalog evidence on title/line proxies — not priced quotes. "
            "False-positive audit: manual review of NEW_MATCH sample required before execution."
        ),
        "WALL_S": round(time.time() - t0, 2),
        "RESULTS": results,
        "NEW_MATCH_IDS": [r["OPPORTUNITY_ID"] for r in new_matches],
        "PACKETS": packets,
    }


# ---------------------------------------------------------------------------
# Multi-line completeness + economics
# ---------------------------------------------------------------------------


def multi_line_completeness(
    *,
    total_required: int,
    lines_identified: int,
    lines_supplier_matched: int,
    lines_price_verified: int,
    lines_financially_covered: int,
    award_structure: str = "",
    partial_permitted: bool | None = None,
) -> dict[str, Any]:
    award = (award_structure or "").upper()
    if partial_permitted is None:
        partial_permitted = any(
            k in award
            for k in ("LINE_ITEM", "PARTIAL_BID", "MULTIPLE_AWARD")
        ) and "ALL_OR_NONE" not in award
    full_ready = (
        total_required > 0
        and lines_identified >= total_required
        and lines_supplier_matched >= total_required
        and lines_price_verified >= total_required
    )
    if not partial_permitted and lines_supplier_matched < total_required:
        full_ready = False
    return {
        "TOTAL_REQUIRED_LINES": total_required,
        "LINES_IDENTIFIED": lines_identified,
        "LINES_SUPPLIER_MATCHED": lines_supplier_matched,
        "LINES_PRICE_VERIFIED": lines_price_verified,
        "LINES_FINANCIALLY_COVERED": lines_financially_covered,
        "FULL_CONTRACT_SOURCING_READY": full_ready,
        "PARTIAL_BIDDING_PERMITTED": partial_permitted,
        "MANDATORY_COVERAGE_PCT": round(100.0 * lines_supplier_matched / total_required, 2) if total_required else 0.0,
    }


def max_supplier_cost_targets(
    *,
    selling_price: Decimal | None,
    freight: Decimal | None = None,
    compliance: Decimal | None = None,
    financing_fees: Decimal | None = None,
) -> dict[str, Any]:
    """Maximum total acquisition cost for profit / margin targets. Conditional when costs unknown."""
    unknown_costs = [n for n, v in (("freight", freight), ("compliance", compliance), ("financing_fees", financing_fees)) if v is None]
    known_direct = (freight or Decimal("0")) + (compliance or Decimal("0")) + (financing_fees or Decimal("0"))
    out: dict[str, Any] = {
        "SELLING_PRICE": _money_str(selling_price),
        "SELLING_PRICE_EVIDENCE": "VERIFIED" if selling_price is not None else "HYPOTHETICAL_OR_UNKNOWN",
        "KNOWN_DIRECT_COSTS": _money_str(known_direct if not unknown_costs else None),
        "UNKNOWN_DIRECT_COSTS": unknown_costs,
        "CONDITIONAL": bool(unknown_costs) or selling_price is None,
        "TARGETS": {},
    }
    if selling_price is None:
        out["NOTE"] = "Cannot compute numeric ceilings without a supported selling-price scenario"
        return out
    for label, profit in (("PROFIT_5K", Decimal("5000")), ("PROFIT_10K", Decimal("10000"))):
        # selling - acquisition - known_direct >= profit  => acquisition <= selling - known_direct - profit
        ceiling = selling_price - known_direct - profit
        out["TARGETS"][label] = {
            "MAX_ACQUISITION_PLUS_UNRESOLVED": _money_str(ceiling),
            "MESSAGE": (
                f"To make ${_money_str(profit)} remaining profit at the evaluated selling price, "
                f"total acquisition plus unresolved costs must stay below ${_money_str(ceiling)}."
            ),
            "CONDITIONAL": bool(unknown_costs),
        }
    for margin_pct in (15, 20, 25, 30):
        # gross margin = (sell - acq) / sell => acq = sell * (1 - m)
        acq = (selling_price * (Decimal(100 - margin_pct) / Decimal(100))).quantize(MONEY)
        out["TARGETS"][f"GROSS_MARGIN_{margin_pct}PCT"] = {
            "MAX_PRODUCT_ACQUISITION": _money_str(acq),
            "NOTE": "Product gross margin target (before freight/financing)",
        }
    return out


def supplier_advantage_score(
    *,
    supplier_count: int,
    verified_acquisition: bool,
    public_price_advantage: Decimal | None = None,
    manufacturer_auth_required: bool = False,
    manufacturer_auth_confirmed: bool = False,
    prior_purchase_price: bool = False,
    credit_terms_verified: bool = False,
    substitution_allowed: bool = False,
) -> dict[str, Any]:
    score = Decimal("0")
    reasons: list[str] = []
    if supplier_count >= 1:
        score += Decimal("15")
        reasons.append("ge1_supplier")
    if supplier_count >= 2:
        score += Decimal("15")
        reasons.append("ge2_suppliers")
    if supplier_count >= 3:
        score += Decimal("10")
        reasons.append("ge3_suppliers")
    if verified_acquisition:
        score += Decimal("25")
        reasons.append("verified_acquisition_price")
    if public_price_advantage is not None and public_price_advantage > 0:
        score += min(Decimal("15"), public_price_advantage.quantize(MONEY))
        reasons.append("public_channel_advantage")
    if manufacturer_auth_required and manufacturer_auth_confirmed:
        score += Decimal("10")
        reasons.append("auth_confirmed")
    elif manufacturer_auth_required and not manufacturer_auth_confirmed:
        score -= Decimal("10")
        reasons.append("auth_missing")
    if prior_purchase_price:
        score += Decimal("10")
        reasons.append("prior_internal_purchase")
    if credit_terms_verified:
        score += Decimal("15")
        reasons.append("credit_terms_verified")
    if substitution_allowed:
        score += Decimal("5")
        reasons.append("substitution_flexibility")
    # Unknown terms must NOT add confirmed financial advantage
    return {
        "SCORE": float(score),
        "REASONS": reasons,
        "HYPOTHETICAL_DISCOUNTS_EXCLUDED": True,
    }


def classify_financial_status(
    *,
    verified_profit: Decimal | None,
    selling_price: Decimal | None,
    acquisition: Decimal | None,
    financing_status: str,
    full_sourcing_ready: bool,
) -> dict[str, Any]:
    labels: list[str] = []
    if selling_price is None:
        labels.append("SELLING_PRICE_EVIDENCE_REQUIRED")
    if acquisition is None:
        labels.append("COST_EVIDENCE_INCOMPLETE")
        labels.append("SUPPLIER_QUOTE_REQUIRED")
    if verified_profit is not None:
        if verified_profit >= Decimal("10000"):
            labels.append("VERIFIED_PROFIT_10K_PLUS")
        elif verified_profit >= Decimal("5000"):
            labels.append("VERIFIED_PROFIT_5K_PLUS")
        elif verified_profit < 0:
            labels.append("NEGATIVE_MARGIN_VERIFIED")
    elif selling_price is not None and acquisition is not None and (selling_price - acquisition) >= Decimal("5000"):
        labels.append("ECONOMICS_PROMISING_UNVERIFIED")
    if financing_status == FIN_BLOCKED:
        labels.append("FINANCING_BLOCKED")
    overall = "RESEARCH"
    if "VERIFIED_PROFIT_10K_PLUS" in labels or "VERIFIED_PROFIT_5K_PLUS" in labels:
        overall = "ECONOMICS_VERIFIED"
    elif "ECONOMICS_PROMISING_UNVERIFIED" in labels:
        overall = "PROMISING_UNVERIFIED"
    elif "NEGATIVE_MARGIN_VERIFIED" in labels:
        overall = "UNPROFITABLE_VERIFIED"
    elif "COST_EVIDENCE_INCOMPLETE" in labels:
        overall = "AWAITING_COST_EVIDENCE"
    if financing_status == FIN_BLOCKED:
        overall = "FINANCING_BLOCKED"
    if not full_sourcing_ready and overall in {"ECONOMICS_VERIFIED", "PROMISING_UNVERIFIED"}:
        overall = "PARTIAL_SOURCING"
    return {"LABELS": labels, "OVERALL_OPERATIONAL_STATUS": overall}


def financing_label_for_deal(
    *,
    contract_value: Decimal | None,
    supplier_cost: Decimal | None,
    owner_cash_required: bool = True,
) -> str:
    """Hard gate: never CONFIRMED without transaction-specific verification."""
    if owner_cash_required and (supplier_cost is None or supplier_cost > 0):
        # No confirmed net terms / PO finance in evidence → path unverified at best
        if supplier_cost is not None and contract_value is not None and contract_value > 0:
            return FIN_UNVERIFIED
        return FIN_UNKNOWN
    # Confirmed only when capital stack assess returns confirmed — not claimed here
    return FIN_UNKNOWN


# ---------------------------------------------------------------------------
# Packet reconciliation
# ---------------------------------------------------------------------------


def reconcile_packets() -> dict[str, Any]:
    raw = _load(HT_PACKETS) or {}
    packets = raw.get("packets") if isinstance(raw, dict) else raw
    packets = [p for p in (packets or []) if isinstance(p, dict)]
    rows = quoteable_rows()
    row_packet_sum = sum(int(r.get("PACKET_COUNT") or 0) for r in rows)
    by_opp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in packets:
        by_opp[str(p.get("OPPORTUNITY_ID") or "")].append(p)

    # Stable identity + duplicate detection
    seen_keys: set[str] = set()
    active: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for p in packets:
        key = f"{p.get('OPPORTUNITY_ID')}|{p.get('SUPPLIER')}|{p.get('OUTREACH_STATE')}"
        if key in seen_keys:
            duplicates.append({**p, "SUPERSEDED": True, "DEDUP_KEY": key})
        else:
            seen_keys.add(key)
            active.append({**p, "PACKET_UID": hashlib.sha1(key.encode()).hexdigest()[:16], "DEDUP_KEY": key})

    quoteable_with = sum(1 for r in rows if int(r.get("PACKET_COUNT") or 0) > 0 or str(r.get("OPPORTUNITY_ID")) in by_opp)
    return {
        "kind": "ci_upgrade_packet_reconciliation",
        "build": BUILD,
        "built_at": _utc(),
        "HISTORICAL_PACKET_FILES": len(packets),
        "UNIQUE_SUPPLIER_PACKETS_ACTIVE": len(active),
        "DUPLICATE_OR_SUPERSEDED": len(duplicates),
        "UNIQUE_OPPORTUNITIES_WITH_PACKETS": len(by_opp),
        "QUOTEABLE_ROWS_PACKET_COUNT_SUM": row_packet_sum,
        "QUOTEABLE_OPPS_WITH_GE1_PACKET": quoteable_with,
        "RECONCILIATION": {
            "packets_file_count": len(packets),
            "active_unique_supplier_packets": len(active),
            "duplicate_superseded": len(duplicates),
            "unique_opportunities": len(by_opp),
            "row_packet_sum": row_packet_sum,
            "EXPLAIN": (
                "371 historical file rows include multi-supplier packets per opportunity plus "
                "duplicate/migrated appends. Active unique (opp,supplier,state) is the operational count."
            ),
        },
        "ACTIVE_PACKETS": active,
        "DUPLICATES": duplicates[:100],
    }


# ---------------------------------------------------------------------------
# Pilot research (bounded)
# ---------------------------------------------------------------------------


def select_pilot(rows: list[dict[str, Any]], *, n: int = 20, rematch: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Prefer open, common-product, rematched or already packet-ready, multi-supplier."""
    rematch_ids = set((rematch or {}).get("NEW_MATCH_IDS") or [])
    # Prefer commercially clear titles even if not yet packet-ready
    commodity_boost = [
        r
        for r in rows
        if re.search(
            r"\b(paper\s*towel|toilet\s*paper|janitorial|toner|office\s+suppl|fastener|"
            r"safety\s+products|glove|filter\s+element|brake|lighting|electrical\s+parts|"
            r"hydrant|pvc|tool)\b",
            str(r.get("TITLE") or ""),
            re.I,
        )
        and str(r.get("PRODUCT_FIT")) != "EXCLUDED"
    ]
    packet_ready = [r for r in rows if int(r.get("PACKET_COUNT") or 0) > 0 or r.get("OPPORTUNITY_ID") in rematch_ids]
    pool_ids = {r.get("OPPORTUNITY_ID") for r in packet_ready}
    pool = list(packet_ready)
    for r in commodity_boost:
        if r.get("OPPORTUNITY_ID") not in pool_ids:
            pool.append(r)
            pool_ids.add(r.get("OPPORTUNITY_ID"))
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in pool or rows:
        title = str(r.get("TITLE") or "").lower()
        score = 0.0
        if r.get("OPPORTUNITY_ID") in rematch_ids:
            score += 50
        if int(r.get("PACKET_COUNT") or 0) > 0:
            score += 40
        if int(r.get("SUPPORTED_SUPPLIER_LINES") or 0) >= 2:
            score += 20
        for kw in ("janitorial", "paper towel", "toilet paper", "toner", "nitrile", "glove"):
            if kw in title:
                score += 40
        for kw in ("electrical", "lighting", "tool", "ppe", "safety", "filter", "mro", "office", "fastener", "brake", "plumbing", "pipe", "hydrant"):
            if kw in title:
                score += 12
        if re.search(r"\b(services?|construction|installation|clean-?up|widening|camera|rubbish)\b", title):
            score -= 60
        # Prefer longer runway
        dl = r.get("DEADLINE")
        try:
            if dl:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(str(dl).replace("Z", "+00:00"))
                days = (dt - now_utc()).total_seconds() / 86400.0
                if days >= 5:
                    score += 15
                elif days >= 3:
                    score += 8
                elif days < 0:
                    score -= 50
        except Exception:
            pass
        if str(r.get("PRODUCT_FIT")) == "EXCLUDED":
            score -= 100
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1].get("OPPORTUNITY_ID") or ""))
    return [r for _, r in scored[:n]]


def research_public_price_bounded(oid: str, identity: dict[str, Any], *, title: str = "") -> dict[str, Any]:
    """Bounded public price research: exact-ID resolver first, then commodity category search.

    Never fabricates prices. Labels evidence levels distinctly. Pack/case ambiguity → uncertain.
    """
    import httpx
    from public_price_search import resolve_public_price
    from public_price_search.catalog_intel import extract_structured_prices
    from public_price_search.domain_map import category_search_urls
    from public_price_search.extract import extract_candidate_from_page, extract_product_links
    from public_price_search.search import fetch_page

    short_desc = (identity.get("synonyms") or [None])[0] or " ".join((identity.get("description") or title or "").split()[:8])
    # 1) Exact-ID path when MPN/model present
    if identity.get("has_exact_id"):
        try:
            pr = resolve_public_price(
                {
                    "opportunity_id": oid,
                    "description": short_desc,
                    "raw_description": identity.get("raw_description") or title,
                    "manufacturer": identity.get("manufacturer"),
                    "part_number": identity.get("part_number"),
                    "model": identity.get("model") or identity.get("part_number"),
                    "nsn": identity.get("nsn"),
                },
                opportunity_id=oid,
                max_queries=5,
                max_pages=8,
                use_budget=True,
            )
            sel = None
            if isinstance(pr.get("search_trace"), dict):
                sel = pr["search_trace"].get("selected_price")
            cand = pr.get("evidence") or sel
            price_val = None
            url = None
            if isinstance(cand, dict):
                price_val = cand.get("price") if cand.get("price") is not None else cand.get("unit_price")
                url = cand.get("url") or cand.get("source_url")
            if price_val is not None:
                return {
                    "status": pr.get("status") or "FOUND",
                    "evidence_level": "EXACT_PUBLIC_PRICE_VERIFIED"
                    if str(pr.get("match_type") or "").upper().startswith("EXACT")
                    else "EQUIVALENT_PUBLIC_PRICE_VERIFIED",
                    "unit_price": float(price_val),
                    "currency": "USD",
                    "source_url": url,
                    "seller": (cand or {}).get("seller") or (cand or {}).get("seller_class"),
                    "checked_at": _utc(),
                    "match_type": pr.get("match_type"),
                    "pack_note": "Normalize to solicitation UOM before using in bid economics",
                }
        except Exception as exc:
            log.info("exact price path error %s: %s", oid, exc)

    # 2) Commodity / description category search (no invented MPN)
    search_ident = {
        "description": short_desc,
        "raw_description": identity.get("raw_description") or title,
        "manufacturer": identity.get("manufacturer"),
        "part_number": identity.get("part_number"),
        "model": identity.get("model"),
    }
    cat_urls = category_search_urls(search_ident, limit=4)
    # Host preference by product family — avoid office retailers for auto/electrical/plumbing
    from urllib.parse import quote_plus

    q = quote_plus(short_desc)
    slug = quote_plus(short_desc.replace(" ", "-"))
    blob = f"{short_desc} {title}".lower()
    office_family = bool(
        re.search(
            r"\b(janitorial|paper\s*towel|toilet\s*paper|toner|office\s+suppl|glove|ppe|"
            r"safety\s+products|safety\s+supplies|nitrile|copier|printer\s+supply)\b",
            blob,
        )
    )
    # Skip service/construction titles — public retail of unrelated catalog items is not evidence
    if re.search(
        r"\b(services?|construction|replacement\s+projects?|widening|clean-?ups?|repair\s+of|"
        r"installation|design[- ]build|cameras?|fencing\s+services?|rubbish|recycling\s+services?|"
        r"waterlines?|water\s+mains?|generators?|turbines?|analyzers?|fuel|lubricants?|"
        r"traffic\s+signs?)\b",
        blob,
    ) and not office_family:
        return {
            "status": "SKIPPED_NON_COMMODITY_TITLE",
            "evidence_level": "PRICE_IDENTITY_UNCERTAIN",
            "unit_price": None,
            "checked_at": _utc(),
            "note": "Title indicates services/construction/specialty — refusing catalog-search price without line MPN",
        }
    if re.search(r"\b(brake|caliper|rotor|alternator|starter|automotive|fleet)\b", blob):
        prefer_hosts = [
            f"https://www.globalindustrial.com/search?q={q}",
            f"https://www.finditparts.com/search?q={q}",
        ]
    elif re.search(r"\b(pipe|valve|hydrant|plumbing|pvc|faucet|gasket|disk)\b", blob):
        prefer_hosts = [
            f"https://www.supplyhouse.com/search?q={q}",
            f"https://www.grainger.com/search?searchQuery={q}",
            f"https://www.mscdirect.com/browse/tn/?searchterm={q}",
            f"https://www.globalindustrial.com/search?q={q}",
        ]
    elif re.search(r"\b(led|lighting|bulbs?|lamps?|fixtures?)\b", blob):
        prefer_hosts = [
            f"https://www.1000bulbs.com/search?q={q}",
            f"https://www.grainger.com/search?searchQuery={q}",
            f"https://www.globalindustrial.com/search?q={q}",
        ]
    elif re.search(r"\b(electrical\s+parts|powerstrut|clamp|cable\s+assembly|cable)\b", blob):
        prefer_hosts = [
            f"https://www.grainger.com/search?searchQuery={q}",
            f"https://www.mscdirect.com/browse/tn/?searchterm={q}",
            f"https://www.globalindustrial.com/search?q={q}",
            f"https://www.graybar.com/search?text={q}",
        ]
    elif re.search(
        r"\b(bearing|housing|sleeve|filter|mop|fastener|charger|battery|air\s+cleaner|"
        r"parts?\s*kit|bracket)\b",
        blob,
    ):
        # Simple industrial / MRO single-line parts — Grainger/MSC/McMaster/Zoro
        prefer_hosts = [
            f"https://www.grainger.com/search?searchQuery={q}",
            f"https://www.mscdirect.com/browse/tn/?searchterm={q}",
            f"https://www.mcmaster.com/search/{q}",
            f"https://www.zoro.com/search?q={q}",
        ]
    elif office_family:
        prefer_hosts = [
            f"https://www.quill.com/search?keywords={q}",
            f"https://www.staples.com/{slug}/directory_{q}",
            f"https://www.officedepot.com/a/search/?q={q}",
            f"https://www.webstaurantstore.com/search/{slug}.html",
        ]
    else:
        # No broad catalog scrape without MPN — avoids false-equivalent prices
        prefer_hosts = []
        if not identity.get("has_exact_id"):
            return {
                "status": "SKIPPED_NEEDS_MPN",
                "evidence_level": "PRICE_IDENTITY_UNCERTAIN",
                "unit_price": None,
                "checked_at": _utc(),
                "note": "No exact MPN/model and title not in commodity office/MRO families",
            }
    urls = []
    for u in prefer_hosts + cat_urls:
        if u not in urls:
            urls.append(u)
    urls = urls[:8]
    # Token overlap required between query and page (blocks cross-category junk hits)
    query_tokens = {w for w in re.findall(r"[a-z0-9]{3,}", short_desc.lower()) if w not in {"and", "the", "for", "with"}}
    blocked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            pages = 0
            for url in urls:
                if pages >= 8:
                    break
                fr = fetch_page(url, client=client, use_budget=True)
                pages += 1
                text = fr.get("text") or ""
                if fr.get("blocked") or not fr.get("ok"):
                    blocked.append({"url": url, "status_code": fr.get("status_code")})
                    continue
                # Prefer product detail links from search pages
                host = fr.get("host") or ""
                detail_urls = extract_product_links(text, search_ident, base_host=host)[:3]
                page_targets = detail_urls or [fr.get("url") or url]
                for target in page_targets:
                    if pages >= 10 and target != (fr.get("url") or url):
                        break
                    if target != (fr.get("url") or url):
                        fr2 = fetch_page(target, client=client, use_budget=True)
                        pages += 1
                        text2 = fr2.get("text") or ""
                        if fr2.get("blocked") or not text2:
                            blocked.append({"url": target, "status_code": fr2.get("status_code")})
                            continue
                        final_url = fr2.get("url") or target
                        body = text2
                    else:
                        final_url = fr.get("url") or url
                        body = text
                    body_l = body.lower()[:200000]
                    overlap = sum(1 for tok in query_tokens if tok in body_l)
                    need = 2 if len(query_tokens) >= 2 else 1
                    if query_tokens and overlap < need:
                        rejected.append({"url": final_url, "reason": "TOKEN_OVERLAP_FAIL", "overlap": overlap})
                        continue
                    # Office hosts must not price non-office families
                    host = (final_url or "").lower()
                    if any(h in host for h in ("quill.com", "staples.com", "officedepot.com")) and not office_family:
                        rejected.append({"url": final_url, "reason": "HOST_FAMILY_MISMATCH"})
                        continue
                    cand = extract_candidate_from_page(body, url=final_url, identity=search_ident)
                    structured = extract_structured_prices(body)[:8]
                    price_val = None
                    if isinstance(cand, dict):
                        price_val = cand.get("unit_price") if cand.get("unit_price") is not None else cand.get("displayed_price")
                    if price_val is None and structured:
                        # Prefer median-ish mid price among structured offers to avoid $1 junk
                        vals = sorted(
                            float(s["price"])
                            for s in structured
                            if s.get("price") is not None and 2.0 <= float(s["price"]) <= 250000
                        )
                        if vals:
                            price_val = vals[len(vals) // 2]
                    if price_val is None:
                        continue
                    try:
                        pv = float(price_val)
                    except (TypeError, ValueError):
                        continue
                    # Reject obvious search-junk / shipping-as-price artifacts
                    if pv < 2.0 or pv > 250000:
                        rejected.append({"url": final_url, "price": pv, "reason": "OUT_OF_RANGE"})
                        continue
                    # Search listing with only structured offers → equivalent commodity, not exact MPN
                    is_search = "/search" in final_url.lower() or "search?" in final_url.lower() or "directory_" in final_url.lower()
                    level = (
                        "EQUIVALENT_PUBLIC_PRICE_VERIFIED"
                        if (structured and overlap >= 2) or not is_search
                        else "PRICE_IDENTITY_UNCERTAIN"
                    )
                    row = {
                        "status": "FOUND",
                        "evidence_level": level,
                        "unit_price": pv,
                        "currency": "USD",
                        "source_url": final_url,
                        "seller": (cand or {}).get("seller") if isinstance(cand, dict) else None,
                        "checked_at": _utc(),
                        "match_type": "COMMODITY_CATEGORY" if not identity.get("has_exact_id") else "DESCRIPTION",
                        "pack_note": "Public list price — verify pack/case vs solicitation UOM before bid",
                        "pages_fetched": pages,
                    }
                    if level == "EQUIVALENT_PUBLIC_PRICE_VERIFIED":
                        return row
                    if best is None:
                        best = row
            if best:
                return best
    except Exception as exc:
        return {
            "status": "ERROR",
            "evidence_level": "EXTERNAL_SOURCE_UNAVAILABLE",
            "unit_price": None,
            "error": f"{type(exc).__name__}: {exc}",
            "checked_at": _utc(),
        }

    return {
        "status": "NOT_FOUND",
        "evidence_level": "PRICE_UNAVAILABLE",
        "unit_price": None,
        "source_url": None,
        "checked_at": _utc(),
        "blocked_sources": blocked[:10],
        "rejected_prices": rejected[:10],
        "urls_tried": urls,
    }


def research_pilot_opportunity(row: dict[str, Any], *, live_price: bool = True) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID") or "")
    title = str(row.get("TITLE") or "")
    # Prefer canonical product-line store, then audit sidecar, over title-only proxy
    sidecar_lines: list[dict[str, Any]] = []
    try:
        from buyer_intelligence.product_line_store_v1 import get_lines as _canonical_lines

        sidecar_lines = _canonical_lines(oid)
    except Exception:
        sidecar_lines = []
    if not sidecar_lines:
        try:
            from buyer_intelligence.source_to_product_extraction_audit_v1 import (
                load_product_lines_for_opportunity,
            )

            sidecar_lines = load_product_lines_for_opportunity(oid)
        except Exception:
            sidecar_lines = []
    if sidecar_lines:
        ln0 = sidecar_lines[0]
        identity = normalize_product_identity(
            str(ln0.get("DESCRIPTION") or title),
            structured={
                "MPN": ln0.get("MPN"),
                "NSN": ln0.get("NSN"),
                "MANUFACTURER": ln0.get("MANUFACTURER"),
                "QTY": ln0.get("QTY"),
                "UOM": ln0.get("UOM"),
            },
        )
        proxy_line = {
            "DESCRIPTION": ln0.get("DESCRIPTION") or title,
            "MANUFACTURER": ln0.get("MANUFACTURER"),
            "BRAND": ln0.get("BRAND"),
            "MPN": ln0.get("MPN"),
            "MODEL": ln0.get("MODEL"),
            "NSN": ln0.get("NSN"),
            "QTY": ln0.get("QTY"),
            "UOM": ln0.get("UOM"),
            "PROVENANCE": "PRODUCT_LINE_V1_SIDECAR",
        }
    else:
        identity = normalize_product_identity(title)
        proxy_line = _proxy_line_from_row(row)
    queries = expand_search_queries(identity)
    fam = classify_family({}, str(proxy_line.get("DESCRIPTION") or title))
    match = best_supplier_for_line(proxy_line, fam, None)
    suppliers = []
    if match.get("SUPPORTED") and match.get("SUPPLIER"):
        suppliers.append(match["SUPPLIER"])
    for alt in match.get("ALTERNATES") or []:
        if alt.get("SUPPLIER"):
            suppliers.append(alt["SUPPLIER"])

    price_result: dict[str, Any] = {
        "status": "SKIPPED",
        "evidence_level": "PRICE_UNAVAILABLE",
        "unit_price": None,
        "source_url": None,
        "checked_at": None,
    }
    if live_price:
        price_result = research_public_price_bounded(oid, identity, title=title)

    # Historical: cache-only, no SAM
    historical = _historical_from_cache(oid, title)

    unit = _dec(price_result.get("unit_price"))
    qty = _dec(identity.get("qty")) or Decimal("1")
    retail_total = (unit * qty) if unit is not None else None
    selling = _dec((historical or {}).get("unit_price")) or _dec((historical or {}).get("award_amount"))
    freight = None  # unknown — must remain unknown
    financing_fees = None
    targets = max_supplier_cost_targets(selling_price=selling, freight=freight, financing_fees=financing_fees)
    advantage = supplier_advantage_score(
        supplier_count=len(suppliers),
        verified_acquisition=False,
        public_price_advantage=None,
        prior_purchase_price=bool(historical and historical.get("unit_price")),
    )
    fin = financing_label_for_deal(contract_value=selling, supplier_cost=retail_total)
    # Verified profit only when BOTH selling and acquisition verified AND freight/financing known
    verified_profit = None
    potential_scenario = None
    if selling is not None and retail_total is not None:
        potential_scenario = {
            "LABEL": "SCENARIO_GROSS_SPREAD_ONLY",
            "GROSS_SPREAD": _money_str(selling - retail_total),
            "NOTE": "Not remaining owner profit — freight/financing/compliance unknown; selling may be historical benchmark only",
        }
    fin_class = classify_financial_status(
        verified_profit=verified_profit,
        selling_price=selling,
        acquisition=retail_total,
        financing_status=fin,
        full_sourcing_ready=False,
    )
    lines_id = len(sidecar_lines) if sidecar_lines else (1 if int(row.get("REAL_LINES") or 0) > 0 else 0)
    ml = multi_line_completeness(
        total_required=max(int(row.get("QUOTEABLE_LINES") or 1), len(sidecar_lines) or 1),
        lines_identified=lines_id,
        lines_supplier_matched=1 if suppliers else 0,
        lines_price_verified=1 if unit is not None else 0,
        lines_financially_covered=0,
        award_structure=str(row.get("AWARD_STRUCTURE") or ""),
    )
    return {
        "OPPORTUNITY_ID": oid,
        "Product": (sidecar_lines[0].get("DESCRIPTION") if sidecar_lines else title),
        "Product_Lines_Recovered": len(sidecar_lines),
        "Product_Line_Contract": "product_line_v1" if sidecar_lines else None,
        "Buyer": row.get("BUYER"),
        "Deadline": row.get("DEADLINE"),
        "Family": fam,
        "Suppliers": suppliers,
        "Past_Gov_Price": historical or {"STATUS": "HISTORICAL_PRICE_UNAVAILABLE"},
        "Current_Retail": price_result,
        "Supplier_Cost": {"STATUS": "UNKNOWN", "NOTE": "No actual supplier quote obtained"},
        "Potential_Profit": {
            "VERIFIED": _money_str(verified_profit),
            "SCENARIO": potential_scenario,
        },
        "Financing": fin,
        "Evidence_Links": [u for u in [price_result.get("source_url"), (historical or {}).get("source_url")] if u],
        "Queries_Expanded": queries[:12],
        "Max_Supplier_Cost_Targets": targets,
        "Supplier_Advantage": advantage,
        "Financial_Classification": fin_class,
        "Multi_Line": ml,
        "Next_Action": _next_action(price_result, historical, suppliers, fin),
    }


def _historical_from_cache(oid: str, title: str) -> dict[str, Any] | None:
    """Search local historical award artifacts — no SAM calls."""
    candidates = [
        "m3_agency_procurement_profiles.json",
        "_og_commercial_research_pipeline_out/m3_bidnet_historical_awards_v1_report.json",
        "data/_og_profit_val_out/m3_opengov_enriched_profitability_validation_v1_results.json",
    ]
    # Direct oid lookups in known small files
    for name in (
        "m3_agency_procurement_profiles.json",
        "_og_commercial_research_pipeline_out/m3_bidnet_historical_awards_v1_report.json",
    ):
        blob = _load(name)
        if not blob:
            continue
        # walk shallow for oid
        found = _find_award_in_blob(blob, oid, title)
        if found:
            return found
    return None


def _find_award_in_blob(blob: Any, oid: str, title: str) -> dict[str, Any] | None:
    if isinstance(blob, dict):
        if str(blob.get("OPPORTUNITY_ID") or blob.get("opportunity_id") or "") == oid:
            amt = blob.get("award_amount") or blob.get("AWARD_AMOUNT") or blob.get("historical_award_total")
            unit = blob.get("unit_price") or blob.get("UNIT_PRICE") or blob.get("historical_unit_price")
            if amt or unit:
                return {
                    "STATUS": "EXACT_HISTORICAL_UNIT_PRICE" if unit else "TOTAL_AWARD_ONLY",
                    "award_amount": amt,
                    "unit_price": unit,
                    "source_url": blob.get("source_url") or blob.get("url"),
                    "buyer": blob.get("buyer") or blob.get("BUYER"),
                    "NOTE": "From cached artifact — not assumed as current win price",
                }
        for v in blob.values():
            if isinstance(v, (dict, list)):
                found = _find_award_in_blob(v, oid, title)
                if found:
                    return found
    elif isinstance(blob, list):
        for item in blob[:500]:
            found = _find_award_in_blob(item, oid, title)
            if found:
                return found
    return None


def _next_action(price: dict[str, Any], hist: dict[str, Any] | None, suppliers: list[str], fin: str) -> str:
    if not suppliers:
        return "Source remaining products — expand supplier match"
    if price.get("evidence_level") in {"PRICE_UNAVAILABLE", "PRICE_IDENTITY_UNCERTAIN", "EXTERNAL_SOURCE_UNAVAILABLE"}:
        return "Research current retail price for exact MPN/model"
    if not hist or hist.get("STATUS") == "HISTORICAL_PRICE_UNAVAILABLE":
        return "Find historical award / bid tabulation"
    if price.get("unit_price") and suppliers:
        return "Request distributor quote with max acquisition ceiling"
    if fin in {FIN_UNKNOWN, FIN_UNVERIFIED}:
        return "Confirm funding path (PO finance / supplier terms) — do not mark confirmed"
    return "Prepare final bid review"


def run_pilot(rows: list[dict[str, Any]], *, live_price: bool = True, rematch: dict[str, Any] | None = None) -> dict[str, Any]:
    pilot_rows = select_pilot(rows, n=20, rematch=rematch)
    results: list[dict[str, Any]] = []
    t0 = time.time()
    for i, row in enumerate(pilot_rows, 1):
        log.info("pilot research %s/%s %s", i, len(pilot_rows), row.get("OPPORTUNITY_ID"))
        results.append(research_pilot_opportunity(row, live_price=live_price))
        _save(CHECKPOINT_FILE, {"stage": "pilot", "done": i, "total": len(pilot_rows), "at": _utc()})
    retail_ok = sum(1 for r in results if r.get("Current_Retail", {}).get("unit_price") is not None)
    hist_ok = sum(
        1
        for r in results
        if isinstance(r.get("Past_Gov_Price"), dict)
        and (r["Past_Gov_Price"].get("unit_price") or r["Past_Gov_Price"].get("award_amount"))
    )
    return {
        "kind": "ci_upgrade_pilot_research",
        "build": BUILD,
        "built_at": _utc(),
        "PILOT_COUNT": len(results),
        "WITH_PUBLIC_RETAIL": retail_ok,
        "WITH_HISTORICAL": hist_ok,
        "VERIFIED_PROFIT_5K": 0,  # freight/financing unknown — never invent
        "VERIFIED_PROFIT_10K": 0,
        "SAM_API_CALLS_USED": 0,
        "WALL_S": round(time.time() - t0, 2),
        "RESULTS": results,
    }


# ---------------------------------------------------------------------------
# Monday outreach queue
# ---------------------------------------------------------------------------


def build_monday_outreach(
    rematch: dict[str, Any],
    pilot: dict[str, Any],
    packet_recon: dict[str, Any],
) -> dict[str, Any]:
    by_supplier: dict[str, dict[str, Any]] = {}

    def _ensure(sup: str) -> dict[str, Any]:
        if sup not in by_supplier:
            meta = SUPPLIER_META.get(sup) or {}
            by_supplier[sup] = {
                "SUPPLIER": sup,
                "CHANNEL_KEY": meta.get("KEY"),
                "CHANNEL_CLASS": meta.get("CHANNEL_CLASS") or "GENERAL_COMMERCIAL",
                "CONTACT": resolve_contact(sup, meta.get("KEY")),
                "OPPORTUNITIES": [],
                "RFQ": {
                    "REQUEST": [
                        "Exact delivered product pricing",
                        "Volume or project discounts",
                        "Manufacturer special/deviated pricing when available",
                        "Freight charges",
                        "Stock and lead times",
                        "Direct-to-government delivery capability",
                        "Payment terms",
                        "New reseller account eligibility",
                        "Manufacturer authorization when needed",
                    ],
                    "INTERNAL_MAX_PRICE_NOTE": "Include internal max acquisition ceilings — do not disclose target profit to supplier",
                    "AUTO_SEND": False,
                },
            }
        return by_supplier[sup]

    # From rematch new packets + active historical
    for p in (rematch.get("PACKETS") or []) + (packet_recon.get("ACTIVE_PACKETS") or []):
        sup = str(p.get("SUPPLIER") or "")
        if not sup:
            continue
        bucket = _ensure(sup)
        oid = str(p.get("OPPORTUNITY_ID") or "")
        if any(o.get("OPPORTUNITY_ID") == oid for o in bucket["OPPORTUNITIES"]):
            continue
        # attach pilot economics if present
        pilot_row = next((r for r in (pilot.get("RESULTS") or []) if r.get("OPPORTUNITY_ID") == oid), None)
        bucket["OPPORTUNITIES"].append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": (pilot_row or {}).get("Product"),
                "BUYER": (pilot_row or {}).get("Buyer"),
                "DEADLINE": (pilot_row or {}).get("Deadline"),
                "MAX_TARGETS": (pilot_row or {}).get("Max_Supplier_Cost_Targets"),
                "NEXT_ACTION": (pilot_row or {}).get("Next_Action") or "Request distributor quote",
            }
        )

    # Prioritize by opportunity count and earliest deadline presence
    ranked = sorted(by_supplier.values(), key=lambda s: (-len(s["OPPORTUNITIES"]), s["SUPPLIER"]))
    return {
        "kind": "ci_upgrade_monday_outreach",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "SUPPLIER_COUNT": len(ranked),
        "SUPPLIERS": ranked[:40],
        "PRIORITY_TOP": [
            {
                "SUPPLIER": s["SUPPLIER"],
                "OPP_COUNT": len(s["OPPORTUNITIES"]),
                "CONTACT_STATE": (s.get("CONTACT") or {}).get("CONTACT_STATE"),
                "FORM_URL": (s.get("CONTACT") or {}).get("CONTACT_URL"),
            }
            for s in ranked[:10]
        ],
    }


# ---------------------------------------------------------------------------
# Available deals projection (UI sync input)
# ---------------------------------------------------------------------------


def build_available_deals(
    rows: list[dict[str, Any]],
    rematch: dict[str, Any],
    pilot: dict[str, Any],
) -> dict[str, Any]:
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    pilot_by = {r["OPPORTUNITY_ID"]: r for r in (pilot.get("RESULTS") or [])}
    cards: list[dict[str, Any]] = []
    for r in rows:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        rm = rematch_by.get(oid)
        pr = pilot_by.get(oid)
        supported = int(r.get("SUPPORTED_SUPPLIER_LINES") or 0)
        if rm:
            supported = max(supported, int(rm.get("SUPPORTED_SUPPLIER_LINES_AFTER") or 0))
        suppliers = list((r.get("SUPPLIER_SET") or {}).keys())
        if rm:
            suppliers = list(dict.fromkeys(suppliers + list((rm.get("SUPPLIER_SET_AFTER") or {}).keys())))
        retail = (pr or {}).get("Current_Retail") or {}
        hist = (pr or {}).get("Past_Gov_Price") or {}
        profit = (pr or {}).get("Potential_Profit") or {}
        fin = (pr or {}).get("Financing") or financing_label_for_deal(contract_value=None, supplier_cost=None)
        advantage = (pr or {}).get("Supplier_Advantage") or supplier_advantage_score(
            supplier_count=len(suppliers), verified_acquisition=False
        )
        # Hide low-value rejects from primary list
        if str(r.get("PRODUCT_FIT")) == "EXCLUDED" and supported == 0:
            continue
        if supported == 0 and oid not in rematch_by and int(r.get("PACKET_COUNT") or 0) == 0:
            continue
        verified_profit = _dec((profit or {}).get("VERIFIED"))
        rank = float(advantage.get("SCORE") or 0)
        if verified_profit is not None:
            rank += 1000 + float(verified_profit)
        elif retail.get("unit_price") is not None:
            rank += 100
        cards.append(
            {
                "opportunity_id": oid,
                "product": r.get("TITLE"),
                "buyer": r.get("BUYER"),
                "deadline": r.get("DEADLINE"),
                "estimated_contract_value": "UNKNOWN",
                "historical_government_pricing": hist if hist else "UNKNOWN",
                "current_retail_cost": retail.get("unit_price") if retail else "UNKNOWN",
                "verified_supplier_cost": "UNKNOWN",
                "supplier_coverage": len(suppliers),
                "suppliers": suppliers[:6],
                "estimated_remaining_profit": profit.get("VERIFIED") or "UNKNOWN",
                "profit_scenario": profit.get("SCENARIO"),
                "financing_status": fin,
                "evidence_confidence": "HIGH"
                if verified_profit is not None
                else ("MEDIUM" if retail.get("unit_price") else "LOW"),
                "supplier_advantage_score": advantage.get("SCORE"),
                "next_action": (pr or {}).get("Next_Action")
                or ("Request distributor quote" if suppliers else "Source products"),
                "workflow": [
                    {"step": "Identify Products", "status": "complete" if int(r.get("REAL_LINES") or 0) > 0 else "incomplete"},
                    {"step": "Verify Prices", "status": "complete" if retail.get("unit_price") else "incomplete"},
                    {"step": "Check Supplier", "status": "complete" if suppliers else "blocked"},
                    {"step": "Calculate Profit", "status": "complete" if verified_profit is not None else "incomplete"},
                    {"step": "Confirm Financing", "status": "blocked" if fin != FIN_CONFIRMED else "complete"},
                    {"step": "Prepare Bid", "status": "incomplete"},
                ],
                "rank_score": rank,
            }
        )
    cards.sort(key=lambda c: (-float(c.get("rank_score") or 0), c.get("deadline") or "9999"))
    return {
        "kind": "ci_upgrade_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(cards),
        "deals": cards[:100],
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_upgrade(
    *,
    sample_size: int = 120,
    rematch_limit: int | None = None,
    live_price: bool = True,
    skip_pilot: bool = False,
) -> dict[str, Any]:
    t0 = time.time()
    rows = load_ht_rows()
    q = quoteable_rows(rows)
    unmatched = unmatched_quoteable(rows)
    baseline = {
        "QUOTEABLE": len(q),
        "NO_SUPPLIER_MATCH": len(unmatched),
        "UNIQUE_OPPS_WITH_PACKETS": sum(1 for r in q if int(r.get("PACKET_COUNT") or 0) > 0),
        "RETAIL_EVIDENCE": 0,
        "HISTORICAL_EVIDENCE": 0,
        "VERIFIED_5K": 0,
        "VERIFIED_10K": 0,
        "FINANCE_CONFIRMED": 0,
        "EXECUTION_READY": 0,
    }
    log.info("baseline quoteable=%s unmatched=%s", baseline["QUOTEABLE"], baseline["NO_SUPPLIER_MATCH"])

    diagnosis = diagnose_unmatched_sample(unmatched, sample_size=sample_size)
    _save(DIAG_FILE, diagnosis)

    rematch = rematch_population(unmatched, limit=rematch_limit)
    _save(REMATCH_FILE, rematch)

    packet_recon = reconcile_packets()
    # Merge new rematch packets into reconciliation view (idempotent by DEDUP_KEY)
    existing_keys = {p.get("DEDUP_KEY") for p in packet_recon.get("ACTIVE_PACKETS") or []}
    merged_new = 0
    for p in rematch.get("PACKETS") or []:
        key = f"{p.get('OPPORTUNITY_ID')}|{p.get('SUPPLIER')}|{p.get('OUTREACH_STATE')}"
        if key not in existing_keys:
            packet_recon.setdefault("ACTIVE_PACKETS", []).append({**p, "DEDUP_KEY": key, "PACKET_UID": hashlib.sha1(key.encode()).hexdigest()[:16]})
            existing_keys.add(key)
            merged_new += 1
    packet_recon["NEW_PACKETS_FROM_REMATCH"] = merged_new
    packet_recon["UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER"] = len(
        {p.get("OPPORTUNITY_ID") for p in packet_recon.get("ACTIVE_PACKETS") or []}
    )
    _save(PACKET_RECON_FILE, packet_recon)

    if skip_pilot:
        pilot = {"kind": "ci_upgrade_pilot_research", "PILOT_COUNT": 0, "RESULTS": [], "WITH_PUBLIC_RETAIL": 0, "WITH_HISTORICAL": 0, "VERIFIED_PROFIT_5K": 0, "VERIFIED_PROFIT_10K": 0, "SAM_API_CALLS_USED": 0}
    else:
        pilot = run_pilot(q, live_price=live_price, rematch=rematch)
    _save(PILOT_FILE, pilot)

    outreach = build_monday_outreach(rematch, pilot, packet_recon)
    _save(OUTREACH_FILE, outreach)

    deals = build_available_deals(q, rematch, pilot)
    _save(DEALS_FILE, deals)

    after = {
        "QUOTEABLE": baseline["QUOTEABLE"],
        "NO_SUPPLIER_MATCH": baseline["NO_SUPPLIER_MATCH"] - int(rematch.get("NEW_LEGITIMATE_MATCHES") or 0),
        "UNIQUE_OPPS_WITH_PACKETS": int(packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER") or packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS") or 0),
        "RETAIL_EVIDENCE": int(pilot.get("WITH_PUBLIC_RETAIL") or 0),
        "HISTORICAL_EVIDENCE": int(pilot.get("WITH_HISTORICAL") or 0),
        "COMPLETE_MULTI_LINE_PRICING": sum(1 for r in (pilot.get("RESULTS") or []) if (r.get("Multi_Line") or {}).get("FULL_CONTRACT_SOURCING_READY")),
        "PLAUSIBLE_5K": sum(
            1
            for r in (pilot.get("RESULTS") or [])
            if (r.get("Financial_Classification") or {}).get("OVERALL_OPERATIONAL_STATUS")
            in {"PROMISING_UNVERIFIED", "ECONOMICS_VERIFIED"}
        ),
        "PLAUSIBLE_10K": 0,
        "VERIFIED_5K": int(pilot.get("VERIFIED_PROFIT_5K") or 0),
        "VERIFIED_10K": int(pilot.get("VERIFIED_PROFIT_10K") or 0),
        "FINANCE_CONFIRMED": 0,
        "EXECUTION_READY": 0,
    }

    report = {
        "kind": "commercial_intelligence_upgrade_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "A_BEFORE_AFTER": {"BEFORE": baseline, "AFTER": after},
        "C_SUPPLIER_MATCHING": {
            "SAMPLE_FAILURE_COUNTS": diagnosis.get("PRIMARY_FAILURE_COUNTS"),
            "ORIGINAL_UNMATCHED": rematch.get("ORIGINAL_UNMATCHED"),
            "SOFTWARE_REPAIRED_NEW_MATCHES": rematch.get("NEW_LEGITIMATE_MATCHES"),
            "REMAINING_UNMATCHED": rematch.get("REMAINING_UNMATCHED"),
            "NEW_PACKET_READY_OPPS": rematch.get("NEW_PACKET_READY_OPPS"),
            "REPAIRS": [
                "Expanded FAMILY_SUPPLIERS (MSC/Fastenal/McMaster/WESCO; other/mro/janitorial)",
                "Improved classify_family (paper towels, safety products, lighting upgrades, fasteners)",
                "Commodity noun elevation to FULL_SPEC_CATEGORY in assess_line_fit",
                "Added WESCO + McMaster-Carr supplier meta",
            ],
        },
        "D_HISTORICAL": {
            "PILOT_WITH_HISTORICAL": pilot.get("WITH_HISTORICAL"),
            "NOTE": "Cache-only; no SAM calls. Aggregate awards never divided into fake unit prices.",
        },
        "E_PRICING": {
            "PILOT_WITH_RETAIL": pilot.get("WITH_PUBLIC_RETAIL"),
            "EVIDENCE_LEVELS_USED": PRICE_LEVELS,
        },
        "F_OUTREACH_PRIORITY": outreach.get("PRIORITY_TOP"),
        "G_ARTIFACTS": {
            "DIAGNOSIS": DIAG_FILE,
            "REMATCH": REMATCH_FILE,
            "PILOT": PILOT_FILE,
            "OUTREACH": OUTREACH_FILE,
            "PACKET_RECON": PACKET_RECON_FILE,
            "AVAILABLE_DEALS": DEALS_FILE,
        },
        "B_PILOT_TABLE": [
            {
                "Product": r.get("Product"),
                "Buyer": r.get("Buyer"),
                "Past_Gov_Price": r.get("Past_Gov_Price"),
                "Current_Retail": r.get("Current_Retail"),
                "Supplier_Cost": r.get("Supplier_Cost"),
                "Potential_Profit": r.get("Potential_Profit"),
                "Financing": r.get("Financing"),
                "Evidence_Links": r.get("Evidence_Links"),
            }
            for r in (pilot.get("RESULTS") or [])
        ],
        "H_REALITY": {
            "SOURCED_AT_VERIFIED_PUBLIC_PRICES": after["RETAIL_EVIDENCE"],
            "VERIFIED_HISTORICAL_BENCHMARKS": after["HISTORICAL_EVIDENCE"],
            "ROOM_FOR_5K_10K_VERIFIED": after["VERIFIED_5K"] + after["VERIFIED_10K"],
            "BEST_SUPPLIER_CHANNELS": outreach.get("PRIORITY_TOP"),
            "NEXT_OPERATOR_ACTIONS": [
                "Open Available Deals (ci_upgrade projection) ranked by evidence",
                "Send Monday RFQs from outreach queue (owner approval required)",
                "Capture actual supplier quotes → recompute verified profit",
                "Confirm financing per transaction before execution-ready",
            ],
        },
    }
    _save(REPORT_FILE, report)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rep = run_upgrade(sample_size=120, rematch_limit=None, live_price=True)
    print(json.dumps({"build": BUILD, "before_after": rep["A_BEFORE_AFTER"], "matching": rep["C_SUPPLIER_MATCHING"], "pilot_retail": rep["E_PRICING"], "wall_s": rep["WALL_S"]}, indent=2))
