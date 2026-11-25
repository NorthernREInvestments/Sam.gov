"""Owner-facing deliverables built from the staged inventory (offline, no supplier contact).

* Supplier research tasks for Sam (directive phase 20): what is known, what is unresolved, what to ask.
* Registration queue (phase 16): portals whose free vendor account gates current opportunities.
* Commercial opportunity table (phase 23): supplier-research-ready separated from commercial research.
* Document retrieval worklist: current product records whose documents automated free retrieval could not
  reach (robots.txt / login), for manual download by a registered vendor.

Nothing here invents a value: unknown fields stay ``None`` and are listed as unresolved.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RESEARCH_QUEUES = ("actionable", "supplier_research_ready", "commercial_research_ready", "financing_verification", "easy_registration")
PRODUCT_PRESENCE = {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}

# Always asked: M3 operates with zero owner cash, no personal guarantee and no personal credit.
_BASE_QUESTIONS = (
    "Is the exact item in stock in the required quantity, and what is the current lead time?",
    "Unit price and any volume price break at the solicitation quantity?",
    "Pack size / unit of measure as sold (confirm it matches the solicitation UOM)?",
    "Freight cost and delivery method to the buyer's delivery point?",
    "Will you drop ship to the government buyer, and blind drop ship (M3 paperwork only)?",
    "Payment terms available to a new business account: Net-30 / Net-45 / Net-60?",
    "Credit application requirements: business-only credit, and is a personal guarantee required?",
    "Country of origin, warranty terms and return policy?",
)


def _days_left(deadline: Any, now: datetime) -> float | None:
    try:
        d = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return round((d - now).total_seconds() / 86400, 1)


def _portal_root(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(str(url))
    if not p.scheme or not p.netloc:
        return None
    if "planetbids.com" in p.netloc and "/portal/" in p.path:
        seg = p.path.split("/portal/", 1)[1].split("/", 1)[0]
        return f"{p.scheme}://{p.netloc}/portal/{seg}/portal-home"
    return f"{p.scheme}://{p.netloc}/"


def supplier_research_task(row: dict[str, Any], now: datetime, price_evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Phase-20 research task for one opportunity. ``row`` is a slim staged-inventory row."""
    ev = _evidence_for(row, price_evidence or [])
    comparable = [e for e in ev if e["match_level"] in _COMPARABLE_LEVELS]
    best_retail = min((e for e in comparable if e["kind"] == "retail"), key=lambda e: float(e["unit_price"]), default=None)
    best_award = min((e for e in comparable if e["kind"] in _GOVERNMENT_KINDS), key=lambda e: float(e["unit_price"]), default=None)
    req = row.get("requirements") or {}
    sreq = req.get("solicitation_requirements") or {}
    lines = req.get("line_items") or []
    src = row.get("sourcing") or {}
    elig = row.get("eligibility") or {}
    first = lines[0] if lines else {}
    unresolved: list[str] = []
    questions = list(_BASE_QUESTIONS)
    if not any(li.get("quantity") is not None for li in lines):
        unresolved.append("quantity_not_stated_in_retained_text")
    if not any(li.get("unit_of_measure") for li in lines):
        unresolved.append("unit_of_measure_not_stated")
    if not sreq.get("delivery"):
        unresolved.append("delivery_requirements_not_found")
    if not sreq.get("certifications"):
        unresolved.append("certification_requirements_not_verified")
    unresolved.append("supplier_unit_cost_unverified")
    if best_award is None:
        unresolved.append("government_price_benchmark_unverified")
    if elig.get("eligibility_state") not in {"ELIGIBLE", "EASY_REGISTRATION_REQUIRED"}:
        unresolved.append("bid_eligibility_terms_unverified")
    if sreq.get("brand_name_only") or src.get("identity_basis") == "title_brand_only":
        questions.append("Brand-name-only requirement: are you an authorized reseller/distributor and can you provide a manufacturer authorization letter?")
    elif sreq.get("brand_name_or_equal"):
        questions.append("Brand name or equal: do you carry the named item or a compliant equal (provide spec sheet for the equal)?")
    else:
        questions.append("Can you supply the named manufacturer/model, and do you have a compliant alternative?")
    for c in (sreq.get("certifications") or [])[:2]:
        questions.append(f"Can you provide documentation for: {c[:120]}?")
    if sreq.get("installation"):
        questions.append("Solicitation mentions installation/setup: can you or a partner perform it, and at what cost?")
    return {
        "staged_id": row.get("staged_id"),
        "owner_queue": row.get("owner_queue"),
        "solicitation_title": row.get("title"),
        "solicitation_number": row.get("solicitation_number"),
        "agency": row.get("buyer"),
        "state": row.get("state"),
        "source": row.get("source"),
        "official_url": row.get("official_url"),
        "deadline_at": row.get("deadline_at"),
        "days_left": _days_left(row.get("deadline_at"), now),
        "product_identity": {"level": src.get("identity_level"), "basis": src.get("identity_basis"),
                             "evidence": src.get("identity_evidence")},
        "line_items": [{k: li.get(k) for k in ("description", "quantity", "unit_of_measure", "pack_size", "manufacturer",
                                               "model", "mpn", "nsn", "brand_name_or_equal", "brand_name_only")} for li in lines[:25]],
        "quantity": first.get("quantity"),
        "unit_of_measure": first.get("unit_of_measure"),
        "manufacturer": first.get("manufacturer"),
        "mpn_or_model": first.get("mpn") or first.get("model"),
        "nsn": first.get("nsn"),
        "approved_alternatives": "brand_name_or_equal" if sreq.get("brand_name_or_equal") else (
            "brand_name_only" if sreq.get("brand_name_only") or src.get("identity_basis") == "title_brand_only" else None),
        "delivery_requirements": sreq.get("delivery"),
        "required_certifications": sreq.get("certifications"),
        "required_lead_time": None,
        "eligibility_state": elig.get("eligibility_state"),
        "registration_required": elig.get("registration_required"),
        "potential_supplier_candidates": _supplier_candidates(row),
        "public_retail_benchmark": _fmt_price(best_retail),
        "historical_award_benchmark": _fmt_price(best_award),
        "price_evidence": ev,
        "indicative_economics": indicative_economics(ev),
        "unresolved": unresolved,
        "supplier_questions": questions,
        "duns_note": "M3's assigned D-U-N-S number is available from the owner when a supplier requests it (not stored here).",
        "next_action": _next_action(row, unresolved),
        "contact_policy": "Research only: no supplier emails or calls are sent by M3.",
    }


PRICE_MATCH_LEVELS = ("EXACT_NSN", "EXACT_MPN", "EXACT_MANUFACTURER_MODEL", "EXACT_BRAND", "COMPLIANT_OR_EQUAL",
                      "GENERIC_SPECIFICATION_MATCH", "UNVERIFIED_MATCH")
_COMPARABLE_LEVELS = set(PRICE_MATCH_LEVELS[:4])
_GOVERNMENT_KINDS = {"award", "bid_tabulation"}


def load_price_evidence(path: Path) -> list[dict[str, Any]]:
    """Researcher-recorded public price observations (``price_evidence.jsonl``).

    Each line: ``kind`` (retail | award | bid_tabulation — a public low bid, award not confirmed),
    ``match_keys`` (lower-case title substrings) and/or ``official_url``,
    ``match_level``, ``unit_price``, ``source_url``, ``observed_at``, ``seller_or_buyer``, ``note``.
    """
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            if e.get("kind") in {"retail", *_GOVERNMENT_KINDS} and e.get("match_level") in PRICE_MATCH_LEVELS and e.get("unit_price") is not None:
                out.append(e)
    return out


def _evidence_for(row: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    title = str(row.get("title") or "").lower()
    url = row.get("official_url")
    return [e for e in evidence if (e.get("official_url") and e["official_url"] == url)
            or any(k and k in title for k in (e.get("match_keys") or []))]


def indicative_economics(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-unit band from public prices only; never PROVEN/LIKELY without supplier quote and full landed cost."""
    comparable = [e for e in evidence if e["match_level"] in _COMPARABLE_LEVELS]
    retail = min((e for e in comparable if e["kind"] == "retail"), key=lambda e: float(e["unit_price"]), default=None)
    award = min((e for e in comparable if e["kind"] in _GOVERNMENT_KINDS), key=lambda e: float(e["unit_price"]), default=None)
    missing = ["supplier_quote", "freight", "sales_tax_treatment", "payment_terms"]
    if retail is None or award is None:
        return {"band": "UNPROVEN", "unit_spread": None, "basis": "needs both a comparable retail price and a historical award price",
                "missing": missing + ([] if retail else ["public_retail_price"]) + ([] if award else ["historical_award_price"])}
    spread = round(float(award["unit_price"]) - float(retail["unit_price"]), 2)
    band = "UNPROFITABLE" if spread <= 0 else "POSSIBLE"
    return {"band": band, "unit_spread": spread,
            "basis": f"lowest historical {award['kind']} {award['unit_price']} minus lowest public retail {retail['unit_price']} per unit"
                     + ("; unprofitable at public retail cost, viable only if a dealer quote beats the award price" if spread <= 0 else ""),
            "missing": missing}


def _fmt_price(e: dict[str, Any] | None) -> str | None:
    if not e:
        return None
    return (f"${float(e['unit_price']):,.2f}/unit {e['match_level']} ({e.get('seller_or_buyer')}, {str(e.get('observed_at'))[:10]}) "
            f"{e.get('source_url')}")


def _supplier_candidates(row: dict[str, Any]) -> list[str]:
    """Supplier channels to research (types, not invented companies)."""
    src = row.get("sourcing") or {}
    ev = str(src.get("identity_evidence") or "").strip()
    out = []
    if src.get("identity_basis") in {"title_brand_model", "title_brand_only", "manufacturer", "model", "mpn"} and ev:
        brand = ev.split()[0]
        out.append(f"{brand} (manufacturer) dealer locator / authorized distributors")
    if src.get("identity_basis") == "nsn":
        out.append("NSN cross-reference (manufacturer CAGE/part) then authorized distributors")
    out.append("National industrial / commercial distributors carrying the product category")
    return out


def _next_action(row: dict[str, Any], unresolved: list[str]) -> str:
    q = row.get("owner_queue")
    if q == "supplier_research_ready":
        return "Sam: request quotes/availability from the supplier candidates using the questions below."
    if "quantity_not_stated_in_retained_text" in unresolved or "bid_eligibility_terms_unverified" in unresolved:
        return ("Download the solicitation documents from the portal (free vendor account) to confirm quantity and bid terms; "
                "meanwhile benchmark public pricing for the identified item.")
    return "Benchmark public pricing for the identified item."


def registration_queue(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Portals whose free vendor account gates current product opportunities (no registration is submitted)."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        elig = r.get("eligibility") or {}
        if not elig.get("registration_required") or r.get("federal_policy_hold"):
            continue
        root = _portal_root(r.get("official_url")) or str(r.get("source"))
        g = groups.setdefault(root, {"portal": root, "source": r.get("source"), "agencies": Counter(), "opportunities": [],
                                     "earliest_deadline": None})
        g["agencies"][r.get("buyer") or "unknown"] += 1
        g["opportunities"].append({"staged_id": r.get("staged_id"), "title": r.get("title"), "deadline_at": r.get("deadline_at"),
                                   "owner_queue": r.get("owner_queue")})
        d = r.get("deadline_at")
        if d and (g["earliest_deadline"] is None or str(d) < str(g["earliest_deadline"])):
            g["earliest_deadline"] = d
    out = []
    for g in groups.values():
        out.append({
            "portal": g["portal"], "source": g["source"], "registration_url": g["portal"],
            "registration_url_basis": "portal root of the solicitation link; the vendor sign-up link is on that page",
            "agencies": dict(g["agencies"].most_common(10)), "opportunity_count": len(g["opportunities"]),
            "opportunities": sorted(g["opportunities"], key=lambda o: str(o["deadline_at"] or "9999"))[:25],
            "earliest_deadline": g["earliest_deadline"], "days_to_earliest_deadline": _days_left(g["earliest_deadline"], now),
            "required_documents": "Typically business legal name, address, EIN/W-9, contact, commodity codes (confirm on portal)",
            "estimated_complexity": "LOW (free self-service vendor account)",
            "owner_involvement_needed": True,
            "owner_involvement_reason": "Account creation accepts portal terms and supplies business identity on M3's behalf",
        })
    return sorted(out, key=lambda g: (-g["opportunity_count"], str(g["earliest_deadline"] or "9999")))


TABLE_COLUMNS = ("solicitation", "agency", "source", "deadline", "days_left", "product", "quantity", "exact_or_equal",
                 "historical_award_comparison", "verified_retail_cost", "gross_spread", "missing_cost_components",
                 "eligibility", "execution_status", "source_link", "next_action", "queue")


def opportunity_table(tasks: list[dict[str, Any]], rows_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tasks:
        r = rows_by_id.get(t["staged_id"]) or {}
        econ = r.get("economics") or {}
        out.append({
            "solicitation": f"{t['solicitation_number'] or ''} {t['solicitation_title'] or ''}".strip(),
            "agency": t["agency"], "source": t["source"], "deadline": t["deadline_at"], "days_left": t["days_left"],
            "product": t["product_identity"]["evidence"] or (t["line_items"][0]["description"] if t["line_items"] else None),
            "quantity": t["quantity"], "exact_or_equal": t["approved_alternatives"] or "not stated",
            "historical_award_comparison": t["historical_award_benchmark"] or "none found in award history",
            "verified_retail_cost": t["public_retail_benchmark"] or "not verified",
            "gross_spread": econ.get("net_profit") if econ.get("profit_class") in {"PROVEN", "LIKELY"} else (
                f"{t['indicative_economics']['band']} {t['indicative_economics']['unit_spread']}/unit (public prices only)"
                if t["indicative_economics"]["unit_spread"] is not None else "unknown"),
            "missing_cost_components": ", ".join(econ.get("missing_inputs") or []),
            "eligibility": t["eligibility_state"], "execution_status": (r.get("execution") or {}).get("execution_state"),
            "source_link": t["official_url"], "next_action": t["next_action"], "queue": t["owner_queue"],
        })
    return out


def document_retrieval_worklist(rows: list[dict[str, Any]], doc_status: dict[str, dict[str, Any]], now: datetime,
                                min_days: float = 3.0, limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("owner_queue") not in {"document_verification", "commercial_research_ready"}:
            continue
        cl = r.get("classification") or {}
        if cl.get("product_presence") not in PRODUCT_PRESENCE or r.get("federal_policy_hold"):
            continue
        dl = _days_left(r.get("deadline_at"), now)
        if dl is None or dl < min_days:
            continue
        st = doc_status.get(str(r.get("source_record_id"))) or {}
        out.append({"staged_id": r.get("staged_id"), "title": r.get("title"), "agency": r.get("buyer"), "source": r.get("source"),
                    "days_left": dl, "presence": cl.get("product_presence"), "evidence": cl.get("evidence_completeness"),
                    "automated_retrieval": st.get("status") or "not_attempted", "retrieval_detail": st.get("reason"),
                    "portal": _portal_root(r.get("official_url")), "official_url": r.get("official_url"), "queue": r.get("owner_queue")})
    rank = {"commercial_research_ready": 0, "document_verification": 1}
    out.sort(key=lambda x: (rank[x["queue"]], 0 if x["presence"] == "EXPLICIT_PRODUCT" else 1, x["days_left"]))
    return out[:limit]


def load_document_status(doc_root: Path) -> dict[str, dict[str, Any]]:
    ck, idmap_p = doc_root / "doc_checkpoint.json", doc_root / "staged_id_map.json"
    if not ck.exists():
        return {}
    done = json.loads(ck.read_text(encoding="utf-8")).get("done") or {}
    idmap = json.loads(idmap_p.read_text(encoding="utf-8")) if idmap_p.exists() else {}
    out: dict[str, dict[str, Any]] = {}
    for sid, st in done.items():
        for src in idmap.get(sid, []):
            out[str(src)] = st
    return out


def build_owner_deliverables(root: Path, rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Write ``root/owner/*``; returns counts. ``rows`` are slim inventory rows with queues assigned."""
    out = root / "owner"
    out.mkdir(parents=True, exist_ok=True)
    rows_by_id = {r["staged_id"]: r for r in rows if r.get("staged_id") and not r.get("duplicate_of")}
    research = [r for r in rows_by_id.values() if r.get("owner_queue") in RESEARCH_QUEUES]
    research.sort(key=lambda r: (RESEARCH_QUEUES.index(r["owner_queue"]), str(r.get("deadline_at") or "9999")))
    prices = load_price_evidence(root / "price_evidence.jsonl")
    tasks = [supplier_research_task(r, now, prices) for r in research]
    with (out / "supplier_research_tasks.jsonl").open("w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
    current = [r for r in rows_by_id.values() if r.get("owner_queue") not in {"archived", "blocked"}]
    reg = registration_queue([r for r in current if (r.get("classification") or {}).get("product_presence") in PRODUCT_PRESENCE], now)
    (out / "registration_queue.json").write_text(json.dumps(reg, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    table = opportunity_table(tasks, rows_by_id)
    with (out / "opportunity_table.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TABLE_COLUMNS)
        w.writeheader()
        w.writerows(table)
    work = document_retrieval_worklist(list(rows_by_id.values()), load_document_status(root / "documents"), now)
    with (out / "document_retrieval_worklist.csv").open("w", encoding="utf-8", newline="") as f:
        if work:
            w = csv.DictWriter(f, fieldnames=list(work[0].keys()))
            w.writeheader()
            w.writerows(work)
    by_queue: dict[str, int] = defaultdict(int)
    for t in tasks:
        by_queue[t["owner_queue"]] += 1
    return {"supplier_research_tasks": len(tasks), "tasks_by_queue": dict(by_queue), "registration_portals": len(reg),
            "price_evidence_records": len(prices), "tasks_with_price_evidence": sum(1 for t in tasks if t["price_evidence"]),
            "indicative_bands": dict(Counter(t["indicative_economics"]["band"] for t in tasks)),
            "document_retrieval_worklist": len(work), "opportunity_table_rows": len(table)}
