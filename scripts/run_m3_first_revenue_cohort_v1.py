"""M3 first-revenue cohort v1 — local reuse only (no OpenAI / SAM spend).

Selects up to 100 open WORTH_RESEARCHING product deals from ADV visibility,
joins existing commercial overlays under _og_commercial_research_pipeline_out,
and writes cohort / top20 / RFQ queue artifacts. Unknown prices stay unknown.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import now_utc  # noqa: E402
from m3_data_root import data_path  # noqa: E402

BUILD = "20261012-m3-first-revenue-cohort-v1"
OUT = "_og_commercial_research_pipeline_out"
ADV = f"{OUT}/m3_adv_v1_available_deals.json"
L23 = "l23_canonical_population_store.json"
COHORT = f"{OUT}/m3_first_revenue_cohort_v1.json"
TOP20 = f"{OUT}/m3_first_revenue_top20_v1.json"
RFQ_Q = f"{OUT}/m3_first_revenue_rfq_queue_v1.json"

AUTO_SEND = False
LIMIT = 100

# Strong common first-revenue product language (do NOT match bare "supply/supplies" alone)
_COMMON = re.compile(
    r"\b("
    r"tool(?:s|kit)?|office\s+furniture|office\s+suppl(?:y|ies)|paper|tissue|towel|"
    r"janitorial|soap|cleaner(?:s)?|disinfectant|bleach|broom|mop|bucket|"
    r"electrical|lighting|led|lamp|bulb|ppe|glove|nitrile|safety\s+glasses|"
    r"hardware|fastener(?:s)?|bolt|screw|nut|washer|clamp|gasket|"
    r"automotive|brake|filter|battery|batteries|mro|consumable|"
    r"toner|cable|wire|outlet|switch|"
    r"oil|grease|lubricant(?:s)?|trash|liner|tape|wiper|hose|fitting|plumbing|"
    r"valve|bearing|pump\s+parts|radiator|paint(?:s)?|"
    r"seal,?plain|encased\s+seal|oil\s+seal"
    r")\b",
    re.I,
)
# FSC classes that are typically commercially sourceable commodities
_FSC_COMMON = re.compile(r"^(51|53|59|61|62|68|75|79|80|81|85|91|25|29|30|31|39|42|45|47|48|52)--")
_SERVICE = re.compile(
    r"\b("
    r"janitorial\s+service|custodial\s+service|grounds?\s+maintenance|lawn\s+care|"
    r"snow\s+removal|construction|renovation|remodel|installation\s+services?|"
    r"professional\s+services?|consulting|staffing|temp\s+labor|"
    r"auction\s+services?|whitepaper|detailed\s+design|feasibility|"
    r"demolish|improvements?\b|detention\s+pond|lease\b|bpa\b|"
    r"on[- ]call\b|repair\s+services?|related\s+services|"
    r"trade\s+contractors?|contractors?\b|school\b|replacement\s+project|"
    r"equipment\s+procurement\s+phase|valve\s+exercising|valve\s+replacement|"
    r"inventory\s+management|control\s+systems|/repair|/services?|"
    r"\bproject\b|auction|cleaning\s*&?\s*inspection|pharmacy\s+cleaning|"
    r"tank\s+cleaning|seal\s+coats?"
    r")\b",
    re.I,
)
_SPECIALTY = re.compile(
    r"\b("
    r"nsn|cage|source\s+approval|oem\s+only|ammunition|munition|weapon|"
    r"hazardous|radioactive|classified|itar|brand\s+name\s+only|"
    r"repair/modification|electrophoresis|leica|cadaver|autopsy|"
    r"ekahau|subscription\s+license|software\s+license|"
    r"aviation\s+fuel|electricity\s+supply|power\s+supply\s+program|"
    r"idexx|laboratory\s+supplies|medical\s+supplies|vehicles?\b|"
    r"police/?security|roofing|sheet\s+metal\s+special|"
    r"medium\s+voltage|epr\s+insulated|waveguide|frequency\s+multiplie|"
    r"industrial\s+generators?|automatic\s+transfer|computer:\s*hardware|"
    r"networking\s+equipment|substation|surge\s+valve\s+access\s+platform|"
    r"\bevse\b"
    r")\b",
    re.I,
)
_INELIGIBLE_SA = re.compile(
    r"\b(hubzone|8\(a\)|sdvosb|sdvob|wosb|edwosb|vosb\s+only|women[- ]owned\s+only|"
    r"service[- ]disabled|abilityone|jwod)\b",
    re.I,
)
_TARGET_MARKET = re.compile(r"\btarget\s+market\b", re.I)
_MULTI_CATALOG = re.compile(
    r"\b(athletic|instructional|library\s+book|fine\s+arts|uniforms?|"
    r"extracurricular|district\s+instructional|cooperative\s+electricity|"
    r"special\s+medical\s+needs)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save(rel: str, payload: dict[str, Any]) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _title_stem(title: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return " ".join(t.split()[:6])


def _index_by_oid(deals: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in deals or []:
        oid = d.get("opportunity_id") or d.get("OPPORTUNITY_ID") or d.get("oid")
        if oid:
            out[str(oid)] = d
    return out


def _as_number(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def _has_acq(blob: dict[str, Any] | None) -> bool:
    if not blob:
        return False
    if blob.get("public_acquisition_cost") not in (None, "", "UNKNOWN"):
        return True
    prices = blob.get("acquisition_prices") or []
    if isinstance(prices, list):
        for p in prices:
            if isinstance(p, dict) and _as_number(p.get("unit_price")) is not None:
                return True
    return False


def _gov_price(blob: dict[str, Any] | None) -> Any:
    if not blob:
        return None
    for k in ("historical_unit", "gov_price", "historical_unit_price"):
        if blob.get(k) not in (None, "", "UNKNOWN"):
            return blob.get(k)
    return None


def _lines_count(adv: dict[str, Any], l23: dict[str, Any] | None, overlays: list[dict[str, Any]]) -> int | None:
    if adv.get("lines") is not None:
        try:
            return int(adv["lines"])
        except Exception:
            pass
    for o in overlays:
        for k in ("lines", "line_count", "mandatory_total"):
            if o.get(k) is not None:
                try:
                    return int(o[k])
                except Exception:
                    pass
        prices = o.get("acquisition_prices")
        if isinstance(prices, list) and prices:
            return len(prices)
    # L23 rarely has explicit line arrays for RAW BidNet rows
    if l23:
        signals = ((l23.get("universe_classification") or {}).get("signals") or {})
        if signals.get("has_line_items_or_schedule") is True:
            # presence known, count unknown
            return None
    return None


def _is_eligible_set_aside(deal: dict[str, Any], l23: dict[str, Any] | None) -> bool:
    if deal.get("eligible") is True or str(deal.get("eligibility_bucket") or "").upper() == "ELIGIBLE":
        # Still exclude explicit restricted language in title/set-aside
        blob = " ".join(
            str(x or "")
            for x in (
                deal.get("product"),
                deal.get("set_aside"),
                deal.get("competition_access_type"),
                (l23 or {}).get("title"),
            )
        )
        if _INELIGIBLE_SA.search(blob):
            return False
        if _TARGET_MARKET.search(str(deal.get("product") or "")):
            return False
        return True
    return False


def _is_common_product(title: str, deal: dict[str, Any]) -> bool:
    # Require strong keyword or common FSC — bare ADV common_product can be supply-only.
    if _COMMON.search(title) or _FSC_COMMON.search(title):
        return True
    return False


def _exclude_reason(deal: dict[str, Any], l23: dict[str, Any] | None) -> str | None:
    title = str(deal.get("product") or (l23 or {}).get("title") or "")
    if str(deal.get("visibility_bucket") or "") != "WORTH_RESEARCHING":
        return "NOT_WORTH_RESEARCHING"
    if str(deal.get("deadline_status") or "").upper() not in {"OPEN_CONFIRMED", "OPEN"}:
        return "DEADLINE_NOT_OPEN_CONFIRMED"
    days = deal.get("days_left")
    if days is None or float(days) < 1:
        return "INSUFFICIENT_RUNWAY"
    if float(days) > 120:
        return "RUNWAY_TOO_LONG_LOW_URGENCY"
    if not _is_eligible_set_aside(deal, l23):
        return "INELIGIBLE_OR_UNKNOWN_SET_ASIDE"
    psc = str(deal.get("product_service_classification") or "").upper()
    if psc in {"PURE_SERVICE", "CONSTRUCTION"}:
        return "NON_PRODUCT"
    if deal.get("specialty_product") or _SPECIALTY.search(title):
        return "SPECIALTY_MILITARY_OEM"
    if _SERVICE.search(title) and not re.search(r"\b(supply|delivery|purchase)\b", title, re.I):
        return "SERVICE_OR_NON_RESALE"
    if _MULTI_CATALOG.search(title) and not _FSC_COMMON.search(title):
        return "BROAD_CATALOG_NOT_FIRST_REVENUE"
    if not _is_common_product(title, deal):
        return "NOT_COMMON_PRODUCT"
    return None


def _priority_score(deal: dict[str, Any], lines: int | None, evidence: dict[str, Any]) -> float:
    days = float(deal.get("days_left") or 0)
    score = float(deal.get("research_priority_score") or 0)
    # Prefer >=5 day runway
    if days >= 14:
        score += 40
    elif days >= 5:
        score += 30
    elif days >= 3:
        score += 10
    else:
        score -= 10
    # Prefer few lines when known
    if lines is None:
        score += 0
    elif lines <= 3:
        score += 25
    elif lines <= 8:
        score += 15
    elif lines <= 15:
        score += 5
    else:
        score -= 20
    title = str(deal.get("product") or "")
    if deal.get("common_product") or _COMMON.search(title):
        score += 15
    if _FSC_COMMON.search(title):
        score += 8
    if evidence.get("with_acq_price"):
        score += 50
    if evidence.get("with_gov_price"):
        score += 40
    if evidence.get("supplier_matches"):
        score += 20
    if evidence.get("rfq_packet_existing"):
        score += 25
    # Prefer concrete FSC/commodity titles over vague "supplies" catalogs
    if re.match(r"^\d{2}--", title):
        score += 12
    if len(title.split()) <= 8 and _COMMON.search(title):
        score += 8
    return score


def _gather_overlays(oid: str, feeds: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    reused: list[str] = []
    new_flags: list[str] = []
    pieces: dict[str, Any] = {}
    for name, idx in feeds.items():
        row = idx.get(oid)
        if row:
            pieces[name] = row
            reused.append(name)
    # Title-stem related prior research (do NOT copy prices across oids)
    stem = None
    for name in ("cgc", "frds"):
        # handled outside via related_titles
        pass
    return {"by_feed": pieces, "evidence_status": "REUSED" if reused else "NEW_NO_LOCAL_EVIDENCE", "reused_feeds": reused, "new_flags": new_flags}


def _missing_evidence(row: dict[str, Any]) -> list[str]:
    missing = []
    if row.get("acquisition_cost") in (None, "unknown", "UNKNOWN"):
        missing.append("acquisition_price")
    if row.get("gov_price") in (None, "unknown", "UNKNOWN"):
        missing.append("historical_gov_price")
    if not row.get("supplier_matches"):
        missing.append("supplier_quote")
    if row.get("financing") in (None, "unknown", "UNKNOWN", "FINANCING_UNKNOWN"):
        missing.append("financing")
    if row.get("lines") is None:
        missing.append("line_schedule")
    return missing


def _next_action(row: dict[str, Any]) -> str:
    miss = row.get("missing_evidence") or []
    if "line_schedule" in miss and "acquisition_price" in miss:
        return "Recover line schedule from solicitation abstract/attachments; then find public acquisition price / prepare RFQ"
    if "acquisition_price" in miss and "historical_gov_price" in miss:
        return "Find acquisition price / request supplier quote; recover historical gov unit price if available"
    if "acquisition_price" in miss:
        return "Find acquisition price / request supplier quote"
    if "historical_gov_price" in miss:
        return "Recover buyer/historical unit price to estimate contribution"
    if "financing" in miss:
        return "Qualify financing after both prices known"
    return "Operator review — commercial evidence incomplete"


def _build_rfq_packet(row: dict[str, Any], l23: dict[str, Any] | None) -> dict[str, Any]:
    title = row["product"]
    products = [
        {
            "line_no": "1",
            "description": title,
            "manufacturer": None,
            "mpn": None,
            "qty": None,
            "uom": None,
            "package": None,
            "NOTE": "Line schedule not locally confirmed; qty/mpn unknown — do not invent",
        }
    ]
    return {
        "OPPORTUNITY_ID": row["opportunity_id"],
        "AUTO_SEND": False,
        "STATUS": "RFQ_PACKET_PREPARED_NEEDS_LINES" if row.get("lines") is None else "RFQ_READY",
        "OPERATOR_STATUS": "READY_FOR_REVIEW",
        "PRIMARY_SUPPLIER": None,
        "SUPPLIER_CONTACT_URL": None,
        "BUYER": row.get("buyer"),
        "SOLICITATION_NUMBER": row.get("solicitation"),
        "SOLICITATION_TITLE": title,
        "BID_DEADLINE": row.get("deadline"),
        "DAYS_LEFT": row.get("days_left"),
        "DELIVERY_DEADLINE": "See solicitation",
        "DELIVERY_ADDRESS": row.get("buyer"),
        "AUTHORITATIVE_URL": row.get("authoritative_url") or (l23 or {}).get("authoritative_url"),
        "PRODUCTS": products,
        "REQUESTED": [
            "Firm delivered unit price to delivery address",
            "Volume/project pricing for full quantity (once qty confirmed)",
            "Freight to destination (if not included)",
            "Lead time / stock availability",
            "Reseller / drop-ship eligibility",
            "Supplier credit / net terms (no personal guarantee)",
            "Quote validity period",
        ],
        "OPERATOR_ACTIONS": [
            "Confirm line schedule from solicitation",
            "Identify supplier",
            "Review RFQ",
            "Copy RFQ",
            "Mark sent (manual confirmation only)",
            "Enter supplier response",
        ],
        "NEXT_ACTION": "Operator review and send — AUTO_SEND=false; opening/copying is NOT sent",
        "EVIDENCE_STATUS": row.get("evidence_status"),
        "MISSING_EVIDENCE": row.get("missing_evidence"),
    }


def main() -> int:
    adv_blob = _load(ADV) or {}
    l23_blob = _load(L23) or {}
    ops = l23_blob.get("opportunities") or {}
    adv_deals = list(adv_blob.get("deals") or [])

    # Overlay indexes (local reuse only)
    cex = _index_by_oid((_load(f"{OUT}/m3_cex_v1_available_deals.json") or {}).get("deals"))
    rse = _index_by_oid((_load(f"{OUT}/m3_rse_v1_available_deals.json") or {}).get("deals"))
    sp = _index_by_oid((_load(f"{OUT}/m3_source_product_v1_available_deals.json") or {}).get("deals"))
    cgc = _index_by_oid((_load(f"{OUT}/m3_cgc_v1_available_deals.json") or {}).get("deals"))
    cgc_res = _index_by_oid((_load(f"{OUT}/m3_cgc_v1_results.json") or {}).get("results") or (_load(f"{OUT}/m3_cgc_v1_results.json") or {}).get("deals"))
    frds = _index_by_oid((_load(f"{OUT}/m3_frds_v1_available_deals.json") or {}).get("deals"))
    saf_rows = (_load(f"{OUT}/m3_saf_v1_advantage_scores.json") or {}).get("rows") or []
    saf: dict[str, dict[str, Any]] = {}
    for r in saf_rows:
        oid = str(r.get("oid") or "")
        if not oid:
            continue
        saf.setdefault(oid, {"opportunity_id": oid, "rows": [], "advantage_class": None})
        saf[oid]["rows"].append(r)
        if r.get("advantage_class"):
            saf[oid]["advantage_class"] = r.get("advantage_class")

    rfq_packets: dict[str, dict[str, Any]] = {}
    for fname in ("m3_cex_v1_rfqs.json", "m3_cgc_v1_rfqs.json", "m3_frds_v1_rfqs.json"):
        blob = _load(f"{OUT}/{fname}") or {}
        for p in blob.get("PACKETS") or blob.get("packets") or []:
            oid = p.get("OPPORTUNITY_ID") or p.get("opportunity_id")
            if oid:
                rfq_packets[str(oid)] = p

    fin_packets: dict[str, dict[str, Any]] = {}
    for fname in (
        "m3_cgc_v1_financing.json",
        "m3_frds_v1_financing.json",
        "m3_financing_qualification_packets_v1.json",
    ):
        blob = _load(f"{OUT}/{fname}") or {}
        for p in blob.get("packets") or blob.get("PACKETS") or []:
            oid = p.get("OPPORTUNITY_ID") or p.get("opportunity_id")
            if oid:
                fin_packets[str(oid)] = p

    feeds = {
        "cex": cex,
        "rse": rse,
        "source_product": sp,
        "cgc": cgc,
        "cgc_results": cgc_res,
        "frds": frds,
        "saf": saf,
        "rfq": rfq_packets,
        "financing": fin_packets,
    }

    # Related prior research by title stem (hint only; no price copy)
    related_by_stem: dict[str, list[dict[str, Any]]] = {}
    for name, idx in (("cgc", cgc), ("frds", frds), ("cgc_results", cgc_res)):
        for oid, row in idx.items():
            st = _title_stem(str(row.get("product") or row.get("title") or ""))
            if not st:
                continue
            related_by_stem.setdefault(st, []).append({"feed": name, "opportunity_id": oid, "product": row.get("product") or row.get("title")})

    exclusion_counts: Counter[str] = Counter()
    scored: list[tuple[float, dict[str, Any]]] = []

    for deal in adv_deals:
        oid = str(deal.get("opportunity_id") or "")
        l23 = ops.get(oid)
        reason = _exclude_reason(deal, l23)
        if reason:
            exclusion_counts[reason] += 1
            continue

        overlay_bits = []
        for name in ("cex", "rse", "source_product", "cgc", "cgc_results", "frds", "saf"):
            if oid in feeds[name]:
                overlay_bits.append(feeds[name][oid])

        gathered = _gather_overlays(oid, {k: feeds[k] for k in ("cex", "rse", "source_product", "cgc", "cgc_results", "frds", "saf", "rfq", "financing")})
        lines = _lines_count(deal, l23, overlay_bits)

        acq = None
        gov = None
        contribution = deal.get("contribution_state") or "CONTRIBUTION_UNKNOWN"
        verified_contrib = deal.get("verified_remaining_contribution")
        conditional_contrib = deal.get("conditional_remaining_contribution")
        financing = deal.get("financing_status") or "UNKNOWN"
        supplier_matches = 0

        for blob in overlay_bits:
            if acq is None and blob.get("public_acquisition_cost") not in (None, ""):
                acq = blob.get("public_acquisition_cost")
            if acq is None and _has_acq(blob):
                prices = blob.get("acquisition_prices") or []
                units = [_as_number(p.get("unit_price")) for p in prices if isinstance(p, dict)]
                units = [u for u in units if u is not None]
                # Do not invent lot totals; surface unit prices only when single known unit, else unknown lot
                if len(units) == 1 and lines in (1, None):
                    acq = units[0]
                elif units:
                    acq = "PARTIAL_LINE_UNIT_PRICES_ONLY"
            g = _gov_price(blob)
            if gov is None and g is not None:
                gov = g
            if blob.get("contribution_state"):
                contribution = blob.get("contribution_state")
            if blob.get("verified_remaining_contribution") is not None:
                verified_contrib = blob.get("verified_remaining_contribution")
            if blob.get("conditional_remaining_contribution") is not None:
                conditional_contrib = blob.get("conditional_remaining_contribution")
            if blob.get("financing_status"):
                financing = blob.get("financing_status")
            if isinstance(blob.get("acquisition_prices"), list):
                supplier_matches += sum(1 for p in blob["acquisition_prices"] if isinstance(p, dict) and (p.get("url") or p.get("supplier")))
            if isinstance(blob.get("suppliers"), list):
                supplier_matches += len(blob["suppliers"])

        if oid in saf:
            supplier_matches += len(saf[oid].get("rows") or [])
        if oid in rfq_packets:
            gathered["reused_feeds"].append("rfq")
            if rfq_packets[oid].get("PRIMARY_SUPPLIER"):
                supplier_matches += 1
        if oid in fin_packets:
            gathered["reused_feeds"].append("financing")
            financing = fin_packets[oid].get("STATUS") or fin_packets[oid].get("status") or financing

        # Visibility feeds are structural reuse even without prices
        if any(f in gathered["reused_feeds"] for f in ("cex", "rse", "source_product")):
            gathered["evidence_status"] = "REUSED"
        elif gathered["reused_feeds"]:
            gathered["evidence_status"] = "REUSED"
        else:
            gathered["evidence_status"] = "NEW_NO_LOCAL_COMMERCIAL_EVIDENCE"

        st = _title_stem(str(deal.get("product") or ""))
        related = [r for r in related_by_stem.get(st, []) if r["opportunity_id"] != oid]
        # Deduplicate related
        seen_rel = set()
        related_u = []
        for r in related:
            key = (r["feed"], r["opportunity_id"])
            if key in seen_rel:
                continue
            seen_rel.add(key)
            related_u.append(r)

        evidence_flags = {
            "with_acq_price": acq not in (None, "PARTIAL_LINE_UNIT_PRICES_ONLY") and _as_number(acq) is not None,
            "with_gov_price": gov is not None and _as_number(gov) is not None,
            "supplier_matches": supplier_matches > 0,
            "rfq_packet_existing": oid in rfq_packets,
        }
        score = _priority_score(deal, lines, evidence_flags)

        potential_profit = None
        if _as_number(acq) is not None and _as_number(gov) is not None:
            # Only if both known as numbers — still no qty => profit unknown at lot level
            potential_profit = "UNKNOWN_NEED_QTY"
        if verified_contrib is not None:
            potential_profit = verified_contrib
        elif conditional_contrib is not None:
            potential_profit = conditional_contrib

        row = {
            "opportunity_id": oid,
            "product": deal.get("product"),
            "buyer": deal.get("buyer"),
            "solicitation": deal.get("solicitation"),
            "deadline": deal.get("deadline"),
            "deadline_raw": deal.get("deadline_raw"),
            "days_left": None if deal.get("days_left") is None else round(float(deal["days_left"]), 2),
            "set_aside": deal.get("set_aside"),
            "competition_access_type": deal.get("competition_access_type"),
            "eligibility_bucket": deal.get("eligibility_bucket"),
            "lines": lines,
            "acquisition_cost": acq if acq is not None else "unknown",
            "gov_price": gov if gov is not None else "unknown",
            "potential_profit": potential_profit if potential_profit is not None else "unknown",
            "contribution_state": contribution,
            "verified_remaining_contribution": verified_contrib,
            "conditional_remaining_contribution": conditional_contrib,
            "financing": financing if financing not in (None, "") else "unknown",
            "supplier_matches": supplier_matches,
            "fully_specified": bool(
                lines is not None
                and lines > 0
                and evidence_flags["with_acq_price"]
                and evidence_flags["with_gov_price"]
            ),
            "with_acq_price": evidence_flags["with_acq_price"],
            "with_gov_price": evidence_flags["with_gov_price"],
            "rfq_packet_existing": evidence_flags["rfq_packet_existing"],
            "evidence_status": gathered["evidence_status"],
            "reused_feeds": sorted(set(gathered["reused_feeds"])),
            "related_prior_research_same_title_stem": related_u[:5],
            "NOTE_RELATED": (
                "Same title-stem prior research exists on OTHER opportunity_ids; prices NOT copied."
                if related_u
                else None
            ),
            "authoritative_url": deal.get("authoritative_url") or (l23 or {}).get("authoritative_url"),
            "source_platform": deal.get("source_platform") or (l23 or {}).get("platform"),
            "visibility_bucket": deal.get("visibility_bucket"),
            "priority_score": round(score, 2),
            "common_product": bool(deal.get("common_product") or _COMMON.search(str(deal.get("product") or ""))),
            "AUTO_SEND": False,
        }
        row["missing_evidence"] = _missing_evidence(row)
        row["next_action"] = _next_action(row)
        scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], x[1].get("days_left") or 0, x[1]["opportunity_id"]))
    # Prefer runway >=5 first, then fill
    primary = [r for s, r in scored if (r.get("days_left") or 0) >= 5]
    secondary = [r for s, r in scored if (r.get("days_left") or 0) < 5]
    selected = (primary + secondary)[:LIMIT]

    # Top 20 actionable table
    top20 = []
    for r in selected[:20]:
        top20.append(
            {
                "product": r["product"],
                "buyer": r["buyer"],
                "solicitation": r["solicitation"],
                "deadline": r["deadline"],
                "days_left": r["days_left"],
                "lines": r["lines"],
                "acquisition_cost": r["acquisition_cost"],
                "gov_price": r["gov_price"],
                "potential_profit": r["potential_profit"],
                "financing": r["financing"],
                "next_action": r["next_action"],
                "missing_evidence": r["missing_evidence"],
                "opportunity_id": r["opportunity_id"],
                "evidence_status": r["evidence_status"],
                "priority_score": r["priority_score"],
            }
        )

    # RFQ-ready candidates: clear commercial product titles, prepare metadata only
    rfq_candidates = []
    for r in selected:
        title = str(r.get("product") or "")
        # Prefer named commodities / FSC hardware; skip if too vague
        if _MULTI_CATALOG.search(title):
            continue
        if not (_COMMON.search(title) or _FSC_COMMON.search(title) or r.get("common_product")):
            continue
        l23 = ops.get(r["opportunity_id"])
        packet = _build_rfq_packet(r, l23)
        # If prior RFQ exists for this oid, reuse it (none expected for ADV set)
        if r["opportunity_id"] in rfq_packets:
            packet = {**rfq_packets[r["opportunity_id"]], "AUTO_SEND": False, "EVIDENCE_STATUS": "REUSED"}
            packet["STATUS"] = rfq_packets[r["opportunity_id"]].get("STATUS") or packet["STATUS"]
        rfq_candidates.append(packet)
        if len(rfq_candidates) >= 25:
            break

    # Counts
    def _count(pred) -> int:
        return sum(1 for r in selected if pred(r))

    summary = {
        "selected": len(selected),
        "fully_specified": _count(lambda r: r.get("fully_specified")),
        "with_acq_price": _count(lambda r: r.get("with_acq_price")),
        "with_gov_price": _count(lambda r: r.get("with_gov_price")),
        "supplier_matches": _count(lambda r: (r.get("supplier_matches") or 0) > 0),
        "rfq_ready": sum(1 for p in rfq_candidates if p.get("STATUS") == "RFQ_READY"),
        "rfq_packets_prepared": len(rfq_candidates),
        "conditional_positive": _count(
            lambda r: str(r.get("contribution_state") or "").upper() == "CONDITIONAL_CONTRIBUTION"
            or (r.get("conditional_remaining_contribution") is not None and _as_number(r.get("conditional_remaining_contribution")) is not None and _as_number(r.get("conditional_remaining_contribution")) > 0)
        ),
        "verified_positive": _count(
            lambda r: str(r.get("contribution_state") or "").upper() == "VERIFIED_CONTRIBUTION"
            or (r.get("verified_remaining_contribution") is not None and _as_number(r.get("verified_remaining_contribution")) is not None and _as_number(r.get("verified_remaining_contribution")) > 0)
        ),
        "financing_ready": _count(
            lambda r: str(r.get("financing") or "").upper() in {"FINANCING_CONFIRMED", "CONFIRMED", "READY", "FINANCING_READY"}
        ),
        "evidence_reused": _count(lambda r: str(r.get("evidence_status") or "").startswith("REUSED")),
        "evidence_new_no_local": _count(lambda r: "NEW" in str(r.get("evidence_status") or "")),
    }

    selection_criteria = {
        "source": ADV,
        "visibility_bucket": "WORTH_RESEARCHING",
        "deadline_status": ["OPEN_CONFIRMED", "OPEN"],
        "days_left": ">=1 preferred; >=5 prioritized; <=120 (exclude multi-year backlog)",
        "eligibility": "ELIGIBLE / unrestricted / open market / total SB only; exclude HUBZone/8(a)/SDVOSB/WOSB-only and Target Market",
        "company_certifications": "NOT HUBZone/8(a)/SDVOSB/WOSB",
        "product": "common commercial (tools/office/janitorial/electrical/lighting/PPE/hardware/fasteners/automotive/MRO) or common FSC",
        "exclude": ["specialty military/OEM", "services", "construction", "broad athletic/instructional catalogs", "ineligible set-asides"],
        "lines": "few lines preferred when known; unknown stays unknown",
        "commercially_sourceable": True,
        "paid_research": "OpenAI/SAM NOT spent; local overlay reuse only",
        "AUTO_SEND": False,
        "budgets": {"openai_usd_day": 1, "sam_calls_day": 10, "other_paid_usd": 0},
        "limit": LIMIT,
        "do_not_invent_prices": True,
    }

    pool_stats = {
        "adv_deals_total": len(adv_deals),
        "worth_researching": sum(1 for d in adv_deals if d.get("visibility_bucket") == "WORTH_RESEARCHING"),
        "scored_after_filters": len(scored),
        "selected": len(selected),
        "exclusion_counts": dict(exclusion_counts),
        "overlay_oid_overlap_with_selected": {
            name: sum(1 for r in selected if r["opportunity_id"] in idx)
            for name, idx in feeds.items()
        },
    }

    cohort = {
        "kind": "m3_first_revenue_cohort_v1",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "selection_criteria": selection_criteria,
        "counts": summary,
        "pool_stats": pool_stats,
        "top20_actionable": top20,
        "rfq_ready_candidates": [
            {
                "opportunity_id": p["OPPORTUNITY_ID"],
                "status": p.get("STATUS"),
                "buyer": p.get("BUYER"),
                "solicitation": p.get("SOLICITATION_NUMBER"),
                "title": p.get("SOLICITATION_TITLE"),
                "deadline": p.get("BID_DEADLINE"),
                "AUTO_SEND": False,
                "evidence_status": p.get("EVIDENCE_STATUS"),
                "next_action": p.get("NEXT_ACTION"),
            }
            for p in rfq_candidates
        ],
        "selected_deals": selected,
        "NOTE": (
            "Commercial price overlays (cgc/frds/saf/rfq) currently have ~0 opportunity_id overlap "
            "with ADV WORTH_RESEARCHING open deals. Prices/profits left unknown unless locally evidenced. "
            "Related same-title-stem prior research noted but prices NOT copied across opportunity_ids."
        ),
    }

    top20_doc = {
        "kind": "m3_first_revenue_top20_v1",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "count": len(top20),
        "rows": top20,
    }

    rfq_doc = {
        "kind": "m3_first_revenue_rfq_queue_v1",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "COUNT": len(rfq_candidates),
        "PACKETS": rfq_candidates,
        "NOTE": "RFQ packet metadata only. AUTO_SEND=false. Do not treat prepare/copy as sent.",
    }

    p1 = _save(COHORT, cohort)
    p2 = _save(TOP20, top20_doc)
    p3 = _save(RFQ_Q, rfq_doc)

    print(
        json.dumps(
            {
                "ok": True,
                "build": BUILD,
                "summary": summary,
                "wrote": [str(p1), str(p2), str(p3)],
                "pool_stats": pool_stats,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
