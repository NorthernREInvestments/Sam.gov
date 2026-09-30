"""Phase L.7 — quote outreach readiness (no send / no outreach)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    STAGE3_NO_ROW_CAP,
)
from phase_l.quote_economics import (
    ACCEPTABLE_QUOTE,
    EXCELLENT_QUOTE,
    FAIL_QUOTE,
    FREIGHT_ESTIMATE,
    FREIGHT_LOW_CONFIDENCE_RESERVE,
    FREIGHT_QUOTE_REQUIRED,
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    MARGINAL_QUOTE,
    QUOTE_DEPENDENT_POSITIVE,
    UOM_UNRESOLVED,
    _f,
    classify_quote_band,
    quote_bands,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD = "20260927-m3-phase-l7-quote-outreach-readiness"
PRODUCT_MEMORY_PATH = ROOT / "data" / "phase_l7_product_memory.json"
SUPPLIER_PERF_MEMORY_PATH = ROOT / "data" / "phase_l7_supplier_perf_memory.json"

READY_FOR_QUOTE_OUTREACH = "READY_FOR_QUOTE_OUTREACH"
OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
APPROVED_FOR_QUOTE_OUTREACH = "APPROVED_FOR_QUOTE_OUTREACH"
SUBMISSION_PATH_REQUIRES_CONFIRMATION = "SUBMISSION_PATH_REQUIRES_CONFIRMATION"
SPEC_MATCH_CANDIDATE = "SPEC_MATCH_CANDIDATE"
STRONG_LEAD_WITHIN_TARGET = "STRONG_LEAD_WITHIN_TARGET"
RECURRING_QUOTE_OPPORTUNITY = "RECURRING_QUOTE_OPPORTUNITY"
L6_EXISTING_POSITIVE = "L6_EXISTING_POSITIVE"
L7_NEWLY_RECOVERED_POSITIVE = "L7_NEWLY_RECOVERED_POSITIVE"

QUOTE_BLOCKED_PRODUCT_IDENTITY = "QUOTE_BLOCKED_PRODUCT_IDENTITY"
QUOTE_BLOCKED_CONFIGURATION = "QUOTE_BLOCKED_CONFIGURATION"
QUOTE_BLOCKED_QUANTITY = "QUOTE_BLOCKED_QUANTITY"
QUOTE_BLOCKED_UOM = "QUOTE_BLOCKED_UOM"
QUOTE_BLOCKED_DESTINATION = "QUOTE_BLOCKED_DESTINATION"
QUOTE_BLOCKED_DEADLINE = "QUOTE_BLOCKED_DEADLINE"
QUOTE_BLOCKED_ELIGIBILITY = "QUOTE_BLOCKED_ELIGIBILITY"
QUOTE_BLOCKED_SOURCE_APPROVAL = "QUOTE_BLOCKED_SOURCE_APPROVAL"
QUOTE_BLOCKED_SOLICITATION_DOCUMENTS = "QUOTE_BLOCKED_SOLICITATION_DOCUMENTS"
QUOTE_BLOCKED_NO_SUPPLIER = "QUOTE_BLOCKED_NO_SUPPLIER"
QUOTE_BLOCKED_GOV_VALUE = "QUOTE_BLOCKED_GOV_VALUE"
QUOTE_BLOCKED_OTHER = "QUOTE_BLOCKED_OTHER"

AUTHORIZED_CONFIRMED = "AUTHORIZED_CONFIRMED"
AUTHORIZED_LIKELY = "AUTHORIZED_LIKELY"
AUTHORIZATION_UNKNOWN = "AUTHORIZATION_UNKNOWN"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
AUTHORIZATION_NOT_REQUIRED = "AUTHORIZATION_NOT_REQUIRED"

QUOTE_EXCELLENT = "QUOTE_EXCELLENT"
QUOTE_ACCEPTABLE = "QUOTE_ACCEPTABLE"
QUOTE_MARGINAL = "QUOTE_MARGINAL"
QUOTE_FAIL = "QUOTE_FAIL"

INTERNAL_FIELDS_NEVER_SUPPLIER = frozenset(
    {
        "government_value",
        "government_historical_price",
        "max_buy",
        "supplier_quote_target",
        "desired_profit",
        "margin_ceiling",
        "ExpectedRevenueLow",
        "ExpectedRevenueMid",
        "ExpectedRevenueHigh",
        "BREAK_EVEN_MAX_BUY",
        "MAX_BUY_FOR_5K_PROFIT",
        "MAX_BUY_FOR_10K_PROFIT",
        "MAX_BUY_FOR_25K_PROFIT",
        "risk_reserve",
        "financing_assumption",
    }
)

assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP


def _utc() -> str:
    return now_utc().isoformat()


def classify_requirement_mode(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    title = str(row.get("title") or "")
    blob = f"{title} {row.get('description') or ''}".lower()
    brand_or_equal = bool(
        re.search(r"\b(or\s+equal|brand[\-\s]?name\s+or\s+equal|equivalent)\b", blob, re.I)
        or commercial.get("brand_or_equal")
    )
    if brand_or_equal:
        mode = "BRAND_OR_EQUAL"
    elif commercial.get("model") or commercial.get("mpn"):
        mode = "EXACT_MANUFACTURER_MODEL"
    elif re.search(r"\b(shall|must|minimum|specification|IAW|salient)\b", blob, re.I):
        mode = "DESCRIPTIVE_SPECIFICATION"
    elif re.search(r"\b(configur|option|trim|upfit)\b", blob, re.I):
        mode = "CONFIGURABLE_EQUIPMENT"
    else:
        mode = "COMMERCIAL_GENERIC"

    bundled = bool(re.search(r"\b(bundle|install|turnkey|warranty\s+package|accessory\s+package)\b", blob, re.I))
    return {
        "requirement_mode": mode,
        "brand_or_equal": brand_or_equal,
        "reference_product": commercial.get("model") or commercial.get("mpn"),
        "manufacturer": commercial.get("manufacturer") or commercial.get("brand"),
        "bundled_or_install": bundled,
        "salient_characteristics": row.get("salient_characteristics") or [],
        "acceptable_substitution_language": (
            "exact reference product OR compliant equivalent clearly identified"
            if brand_or_equal
            else "exact product as specified"
        ),
        "spec_match_status": SPEC_MATCH_CANDIDATE if mode == "DESCRIPTIVE_SPECIFICATION" else None,
    }


def classify_freight_for_quote(row: dict[str, Any], freight: dict[str, Any] | None = None) -> dict[str, Any]:
    freight = freight or {}
    title = str(row.get("title") or "").lower()
    dest = str(
        row.get("place_of_performance")
        or row.get("delivery_location")
        or row.get("delivery_state")
        or row.get("state")
        or ""
    )
    if re.search(r"\b(alaska|hawaii|ak\b|hi\b)\b", dest + " " + title, re.I):
        cls = "ALASKA_HAWAII"
    elif re.search(r"\b(vehicle|truck|trailer|excavator|loader|forklift|toolcat|bobcat)\b", title, re.I):
        cls = "VEHICLE_EQUIPMENT_TRANSPORT"
    elif re.search(r"\b(ftl|full\s+truck|truckload)\b", title, re.I):
        cls = "FULL_TRUCKLOAD_POSSIBILITY"
    elif re.search(r"\b(ltl|pallet|crate)\b", title, re.I):
        cls = "LTL_LIKELY"
    elif re.search(r"\b(laptop|switch|server|printer|furniture)\b", title, re.I):
        cls = "PARCEL"
    elif not dest.strip():
        cls = "FREIGHT_UNKNOWN"
    else:
        cls = "UNUSUAL_OR_STANDARD"
    return {
        "freight_class": cls,
        "destination": dest or None,
        "status": freight.get("status") or FREIGHT_LOW_CONFIDENCE_RESERVE,
        "amount_reserve": freight.get("amount"),
        "invented": False,
    }


def classify_supplier_authorization(candidate: dict[str, Any]) -> str:
    explicit = str(candidate.get("authorized_status") or candidate.get("authorization_state") or "").upper()
    if explicit in {AUTHORIZED_CONFIRMED, "CONFIRMED", "YES"}:
        return AUTHORIZED_CONFIRMED
    if explicit in {NOT_AUTHORIZED, "NO", "DENIED"}:
        return NOT_AUTHORIZED
    if explicit in {AUTHORIZATION_NOT_REQUIRED, "NOT_REQUIRED", "N/A"}:
        return AUTHORIZATION_NOT_REQUIRED
    role = str(candidate.get("supplier_role") or "").upper()
    st = str(candidate.get("source_type") or "").upper()
    if "COOPERATIVE" in st or st in {"MRO", "RESELLER"} and "OEM" not in st:
        if "COOPERATIVE" in st:
            return AUTHORIZATION_NOT_REQUIRED
    if "OEM" in role or "AUTHORIZED" in role or "AUTHORIZED" in st or st == "OEM":
        return AUTHORIZED_LIKELY
    if candidate.get("authorization_evidence"):
        return AUTHORIZED_LIKELY
    return AUTHORIZATION_UNKNOWN


def rank_suppliers_for_quote(
    suppliers: list[dict[str, Any]],
    *,
    commercial: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    commercial = commercial or {}
    memory = (supplier_memory or {}).get("suppliers") or {}
    ranked = []
    for i, s in enumerate(suppliers or []):
        auth = classify_supplier_authorization(s)
        domain = str(s.get("supplier_domain") or s.get("domain") or s.get("name") or "").lower()
        mem = memory.get(domain) or {}
        score = 0
        factors = []
        if auth == AUTHORIZED_CONFIRMED:
            score += 40
            factors.append("authorized_confirmed")
        elif auth == AUTHORIZED_LIKELY:
            score += 28
            factors.append("authorized_likely")
        elif auth == NOT_AUTHORIZED:
            score -= 20
            factors.append("not_authorized")
        if s.get("exact_product_evidence") or s.get("product_fit") == "EXACT":
            score += 25
            factors.append("exact_product")
        elif s.get("product_fit") in {"FAMILY", "STRONG"} or commercial.get("manufacturer"):
            score += 12
            factors.append("family_fit")
        if s.get("government_sales") or s.get("fleet_sales"):
            score += 10
            factors.append("gov_fleet_fit")
        if s.get("public_quote_path") or s.get("contact_path"):
            score += 8
            factors.append("quote_path")
        if mem.get("responsiveness") or mem.get("quote_quality"):
            score += 10
            factors.append("prior_m3_history")
        if s.get("geo_suitable") or s.get("location_fit"):
            score += 6
            factors.append("geo")
        if s.get("terms_clue") or s.get("financing_clue"):
            score += 4
            factors.append("terms")
        # Do NOT boost merely for discovery order
        ranked.append(
            {
                **s,
                "authorization_state": auth,
                "supplier_rank_score": score,
                "rank_factors": factors,
                "discovery_order": i,
            }
        )
    ranked.sort(key=lambda x: (-(x.get("supplier_rank_score") or 0), x.get("discovery_order") or 0))
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return ranked


def hard_eligibility_blockers(
    row: dict[str, Any], *, lane: str | None = None, commercial: dict[str, Any] | None = None
) -> list[str]:
    commercial = commercial or {}
    blob = " ".join(
        str(x or "")
        for x in (row.get("title"), row.get("description"), row.get("set_aside"), row.get("naics"))
    ).lower()
    blockers: list[str] = []
    if lane in {SOURCE_APPROVAL_REQUIRED} or re.search(r"source\s+approval|qpl|qml|approved\s+source", blob):
        blockers.append(QUOTE_BLOCKED_SOURCE_APPROVAL)
    if lane in {SOLE_SOURCE_RESTRICTED} or re.search(r"sole[\-\s]?source|only\s+one\s+responsible", blob):
        blockers.append(QUOTE_BLOCKED_ELIGIBILITY)
    if re.search(r"\b(8[\-\s]?a\s+only|hubzone\s+only|sdvosb\s+only|wosb\s+only)\b", blob):
        if not row.get("set_aside_eligible"):
            blockers.append(QUOTE_BLOCKED_ELIGIBILITY)
    if re.search(r"mandatory\s+(gsa|sewp|nasa\s+sewp|contract\s+vehicle)", blob):
        if not row.get("holds_required_vehicle"):
            blockers.append(QUOTE_BLOCKED_ELIGIBILITY)
    if re.search(r"manufacturer\s+authorization\s+required", blob) and not commercial.get("authorization_available"):
        blockers.append(QUOTE_BLOCKED_ELIGIBILITY)
    return blockers


def quote_runway(deadline_days: float | None, *, min_days_for_ready: float = 5.0) -> dict[str, Any]:
    if deadline_days is None:
        return {
            "days": None,
            "status": "DEADLINE_UNKNOWN",
            "enough_for_quote": True,  # unknown — do not auto-block
            "penalty": 0,
        }
    d = float(deadline_days)
    if d < 3:
        return {"days": d, "status": "TOO_SHORT", "enough_for_quote": False, "penalty": 40}
    if d < min_days_for_ready:
        return {"days": d, "status": "CONSTRAINED", "enough_for_quote": False, "penalty": 25}
    if d < 7:
        return {"days": d, "status": "URGENT", "enough_for_quote": True, "penalty": 10}
    if d < 14:
        return {"days": d, "status": "VIABLE", "enough_for_quote": True, "penalty": 0}
    return {"days": d, "status": "HIGH_FEASIBILITY", "enough_for_quote": True, "penalty": 0}


def build_internal_quote_control(ev: dict[str, Any]) -> dict[str, Any]:
    """Internal-only economics — never merge into supplier packet."""
    mb = ev.get("max_buy") or {}
    th = mb.get("thresholds") or {}
    gov = ev.get("government_value") or {}
    rev = ev.get("expected_revenue") or {}
    return {
        "kind": "InternalQuoteControl",
        "supplier_facing": False,
        "government_value": gov.get("unit_value") or gov.get("total_value"),
        "government_value_state": gov.get("state"),
        "expected_revenue_range": {
            "low": rev.get("ExpectedRevenueLow"),
            "mid": rev.get("ExpectedRevenueMid"),
            "high": rev.get("ExpectedRevenueHigh"),
        },
        "break_even_max_buy": th.get("BREAK_EVEN_MAX_BUY"),
        "max_buy_5k": th.get("MAX_BUY_FOR_5K_PROFIT"),
        "max_buy_10k": th.get("MAX_BUY_FOR_10K_PROFIT"),
        "max_buy_25k": th.get("MAX_BUY_FOR_25K_PROFIT"),
        "margin_max_buys": {
            "15pct": th.get("MAX_BUY_FOR_15_PERCENT_MARGIN"),
            "20pct": th.get("MAX_BUY_FOR_20_PERCENT_MARGIN"),
            "25pct": th.get("MAX_BUY_FOR_25_PERCENT_MARGIN"),
        },
        "supplier_quote_target": mb.get("supplier_quote_target"),
        "freight_reserve": (ev.get("freight") or {}).get("amount"),
        "financing_assumption": mb.get("financing_rate") or 0.05,
        "risk_reserve": mb.get("risk_reserve_rate"),
    }


def build_supplier_facing_packet(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
    requirement: dict[str, Any] | None = None,
    freight_info: dict[str, Any] | None = None,
    supplier: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
    uom: dict[str, Any] | None = None,
    internal_deadline_days: float | None = None,
) -> dict[str, Any]:
    """Supplier-facing packet — must NOT include max-buy / profit / gov history."""
    commercial = commercial or {}
    requirement = requirement or classify_requirement_mode(row, commercial)
    uom = uom or {}
    original = original or {}
    packet = {
        "kind": "SupplierFacingQuotePacket",
        "phase": "L.7",
        "send_authorized": False,
        "outreach_authorized": False,
        "supplier_name": (supplier or {}).get("name") or (supplier or {}).get("supplier_domain"),
        "solicitation_number": original.get("solicitation_number")
        or row.get("solicitation_id")
        or row.get("notice_id"),
        "government_buyer": original.get("issuing_agency") or row.get("agency"),
        "product_description": (row.get("title") or "")[:300],
        "manufacturer": commercial.get("manufacturer") or commercial.get("brand"),
        "model": commercial.get("model"),
        "mpn_sku": commercial.get("mpn") or commercial.get("sku"),
        "quantity": uom.get("quantity") or row.get("quantity"),
        "uom": uom.get("uom"),
        "acceptable_equivalents": requirement.get("acceptable_substitution_language"),
        "required_specifications": row.get("specifications") or requirement.get("salient_characteristics"),
        "required_condition": row.get("condition") or "new",
        "warranty": row.get("warranty"),
        "delivery_destination": freight_info.get("destination") if freight_info else None,
        "required_delivery_date": row.get("delivery_date") or row.get("required_delivery"),
        "fob_terms": row.get("fob") or row.get("fob_terms"),
        "packaging_pallet_requirements": row.get("packaging"),
        "country_of_origin_taa_baa": row.get("taa") or row.get("baa") or row.get("country_of_origin_requirement"),
        "internal_quote_deadline_note": (
            f"Please respond within {max(2, int((internal_deadline_days or 7) // 2))} business days"
            if internal_deadline_days is not None
            else "Please respond promptly"
        ),
        "pricing_request": "Please provide best commercial unit and total pricing, lead time, and freight estimate if available.",
    }
    # Hard guard: strip any accidental internal keys
    for k in list(packet.keys()):
        if k in INTERNAL_FIELDS_NEVER_SUPPLIER or "max_buy" in k.lower() or "profit" in k.lower():
            if k not in {
                "kind",
                "phase",
                "send_authorized",
                "outreach_authorized",
                "pricing_request",
                "internal_quote_deadline_note",
            }:
                del packet[k]
    assert "supplier_quote_target" not in packet
    assert "max_buy" not in packet
    assert "government_value" not in packet
    return packet


def evaluate_supplier_quote_response(
    *,
    quoted_unit: float | None,
    quantity: float = 1.0,
    freight: float = 0.0,
    financing_rate: float = 0.05,
    other_fees: float = 0.0,
    revenue_mid: float | None = None,
    max_buy: dict[str, Any] | None = None,
    quote_date: str | None = None,
    expiration_date: str | None = None,
    valid_through: str | None = None,
    lead_time_days: float | None = None,
    price_subject_to_change: bool = False,
) -> dict[str, Any]:
    """Logic for later quote ingestion — no live quotes in L.7."""
    unit = _f(quoted_unit)
    qty = float(quantity or 1.0)
    if unit is None:
        return {"state": None, "error": "missing_quote"}
    acq_total = unit * qty
    financing = acq_total * float(financing_rate or 0)
    landed = acq_total + float(freight or 0) + financing + float(other_fees or 0)
    rev = _f(revenue_mid)
    gross = (rev - acq_total) if rev is not None else None
    net = (rev - landed) if rev is not None else None
    margin = (net / rev) if rev and net is not None else None
    bands = quote_bands(max_buy)
    band = classify_quote_band(unit if (max_buy or {}).get("basis") != "TOTAL" else acq_total, bands)
    mapping = {
        EXCELLENT_QUOTE: QUOTE_EXCELLENT,
        ACCEPTABLE_QUOTE: QUOTE_ACCEPTABLE,
        MARGINAL_QUOTE: QUOTE_MARGINAL,
        FAIL_QUOTE: QUOTE_FAIL,
    }
    expired = False
    # Simple ISO date compare if both present
    if expiration_date and quote_date and str(expiration_date) < str(quote_date):
        expired = True
    return {
        "quoted_unit_cost": unit,
        "total_acquisition_cost": round(acq_total, 2),
        "freight": freight,
        "financing": round(financing, 2),
        "total_landed_cost": round(landed, 2),
        "gross_spread": round(gross, 2) if gross is not None else None,
        "expected_net": round(net, 2) if net is not None else None,
        "margin": round(margin, 4) if margin is not None else None,
        "quote_date": quote_date,
        "expiration_date": expiration_date,
        "valid_through": valid_through or expiration_date,
        "lead_time_days": lead_time_days,
        "price_subject_to_change": price_subject_to_change,
        "expired": expired,
        "usable_as_final_acquisition_evidence": bool(not expired and band in {EXCELLENT_QUOTE, ACCEPTABLE_QUOTE}),
        "classification": mapping.get(band or "", QUOTE_FAIL if band else None),
        "band": band,
    }


def compare_supplier_quotes(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank multiple quotes — cheapest is not automatically best."""
    scored = []
    for q in quotes or []:
        if q.get("expired"):
            score = -1000
        else:
            landed = _f(q.get("total_landed_cost")) or 1e18
            score = 0
            # Prefer lower landed cost but weight compliance/auth/lead time
            score -= min(landed / 1000.0, 500)
            if q.get("classification") == QUOTE_EXCELLENT:
                score += 50
            elif q.get("classification") == QUOTE_ACCEPTABLE:
                score += 30
            elif q.get("classification") == QUOTE_MARGINAL:
                score += 5
            if q.get("authorization_state") == AUTHORIZED_CONFIRMED:
                score += 20
            elif q.get("authorization_state") == AUTHORIZED_LIKELY:
                score += 10
            lt = _f(q.get("lead_time_days"))
            if lt is not None and lt <= 14:
                score += 8
            if q.get("payment_terms_favorable"):
                score += 5
            if q.get("usable_as_final_acquisition_evidence"):
                score += 15
        scored.append({**q, "comparison_score": round(score, 2)})
    scored.sort(key=lambda x: -(x.get("comparison_score") or 0))
    for i, s in enumerate(scored):
        s["comparison_rank"] = i + 1
    return scored


def quote_outreach_priority_score(
    *,
    ev: dict[str, Any],
    runway: dict[str, Any],
    eligibility_ok: bool,
    recurring: bool = False,
    strong_lead: bool = False,
    original_complete: bool = False,
) -> dict[str, Any]:
    score = 0
    factors: list[str] = []
    qdep = ev.get("quote_dependent") or {}
    tiers = qdep.get("tiers") or {}
    if tiers.get("ge_50k"):
        score += 35
        factors.append("profit_50k")
    elif tiers.get("ge_25k"):
        score += 28
        factors.append("profit_25k")
    elif tiers.get("ge_10k"):
        score += 20
        factors.append("profit_10k")
    elif tiers.get("ge_5k"):
        score += 14
        factors.append("profit_5k")
    elif tiers.get("quote_dependent_positive"):
        score += 8
        factors.append("profit_positive")

    gov = (ev.get("government_value") or {}).get("state")
    if gov == GOV_VALUE_EXACT:
        score += 20
    elif gov == GOV_VALUE_STRONG:
        score += 15
    elif gov == GOV_VALUE_RANGE:
        score += 10
    elif gov == GOV_VALUE_COMPARABLE:
        score += 6
    factors.append(f"gov_{gov}")

    n = int(ev.get("supplier_count") or len(ev.get("suppliers") or []))
    score += 15 if n >= 3 else 10 if n >= 2 else 5 if n >= 1 else 0
    score -= int(runway.get("penalty") or 0)
    factors.append(f"runway_{runway.get('status')}")
    if eligibility_ok:
        score += 10
    else:
        score -= 30
    if recurring:
        score += 12
        factors.append("recurring")
    if strong_lead:
        score += 8
        factors.append("strong_lead_within")
    if original_complete:
        score += 8
        factors.append("original_complete")
    freight = (ev.get("freight") or {}).get("status")
    if freight == FREIGHT_QUOTE_REQUIRED:
        score -= 6
        factors.append("hard_freight")
    return {"score": max(0, min(100, score)), "factors": factors}


def infer_quantity_from_text(row: dict[str, Any]) -> float | None:
    """Parse quantity hints from title/description when structured qty missing."""
    q = _f(row.get("quantity"))
    if q and q > 0:
        return q
    blob = f"{row.get('title') or ''} {row.get('description') or ''}"
    patterns = (
        r"\bqty\.?\s*[:=]?\s*(\d+)",
        r"\bquantity\s*[:=]?\s*(\d+)",
        r"\b(?:one|1)\s*\(\s*1\s*\)",
        r"\b\(\s*(\d+)\s*\)",
        r"\b(\d+)\s*(?:each|ea|units?|vehicles?|trucks?|buses?)\b",
        r"\bpurchase\s+of\s+(?:one|1)\b",
    )
    for pat in patterns:
        m = re.search(pat, blob, re.I)
        if m:
            if m.lastindex:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    return 1.0
            return 1.0
    if re.search(r"\b(one|a)\s+(new|unused|current)?\s*(vehicle|truck|loader|forklift|trailer|bus)\b", blob, re.I):
        return 1.0
    return None


def evaluate_quote_readiness(
    row: dict[str, Any],
    *,
    ev: dict[str, Any],
    lane: str | None = None,
    commercial: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
    submission: dict[str, Any] | None = None,
    deadline_days: float | None = None,
    was_l6_positive: bool = False,
    recurring: bool = False,
) -> dict[str, Any]:
    """Gate READY_FOR_QUOTE_OUTREACH — does not send."""
    commercial = commercial or {}
    original = original or {}
    submission = submission or {}
    blockers: list[str] = []

    requirement = classify_requirement_mode(row, commercial)
    freight_info = classify_freight_for_quote(row, ev.get("freight"))
    suppliers = rank_suppliers_for_quote(ev.get("suppliers") or [], commercial=commercial)
    uom = ev.get("uom") or {}
    gov = ev.get("government_value") or {}
    max_buy = ev.get("max_buy") or {}
    qdep = ev.get("quote_dependent") or {}
    tiers = qdep.get("tiers") or {}

    # Product identity
    if not (
        commercial.get("model")
        or commercial.get("mpn")
        or commercial.get("manufacturer")
        or requirement["requirement_mode"] in {"DESCRIPTIVE_SPECIFICATION", "BRAND_OR_EQUAL"}
        or (row.get("title") and len(str(row.get("title"))) >= 12)
    ):
        blockers.append(QUOTE_BLOCKED_PRODUCT_IDENTITY)

    if requirement.get("bundled_or_install") and not row.get("configuration_resolved"):
        # Soft: configuration adequate if brand/model known
        if not (commercial.get("model") or commercial.get("mpn")):
            blockers.append(QUOTE_BLOCKED_CONFIGURATION)

    qty = uom.get("quantity") or row.get("quantity") or infer_quantity_from_text(row)
    # Unit-basis quote OK when product is defined enough for a price request
    product_defined = bool(
        commercial.get("model")
        or commercial.get("mpn")
        or commercial.get("manufacturer")
        or requirement["requirement_mode"] in {"BRAND_OR_EQUAL", "EXACT_MANUFACTURER_MODEL"}
        or re.search(
            r"\b(ford|bobcat|dell|cisco|kubota|chevrolet|vehicle|truck|suv|forklift|trailer|laptop)\b",
            str(row.get("title") or ""),
            re.I,
        )
    )
    if not qty and not product_defined and uom.get("status") == UOM_UNRESOLVED:
        blockers.append(QUOTE_BLOCKED_QUANTITY)
    elif not qty and product_defined:
        # Explicit unit-basis allowance — do not block
        qty_basis_ok = True
        uom = {**uom, "quantity_basis": "UNIT_BASIS_ALLOWED", "inferred_quantity": None}
    else:
        qty_basis_ok = True
        if qty and not uom.get("quantity"):
            uom = {**uom, "quantity": qty, "quantity_inferred": True}

    if uom.get("status") == UOM_UNRESOLVED and gov.get("unit_value") and gov.get("total_value"):
        blockers.append(QUOTE_BLOCKED_UOM)

    # Destination: only block when freight class needs dest and none present
    if freight_info["freight_class"] in {"VEHICLE_EQUIPMENT_TRANSPORT", "ALASKA_HAWAII"} and not freight_info.get(
        "destination"
    ):
        # Prefer warning over hard block if solicitation still quoteable for pickup/FOB
        if not row.get("fob") and not row.get("fob_terms"):
            pass  # do not hard-block — freight classified unknown/reserve

    runway = quote_runway(deadline_days)
    if not runway["enough_for_quote"] and deadline_days is not None:
        blockers.append(QUOTE_BLOCKED_DEADLINE)

    elig = hard_eligibility_blockers(row, lane=lane, commercial=commercial)
    blockers.extend(elig)

    if lane == MILSPEC_SPECIALTY and not suppliers:
        # Specialty without suppliers — not ready (retain, cheap track)
        blockers.append(QUOTE_BLOCKED_NO_SUPPLIER)

    if not suppliers:
        blockers.append(QUOTE_BLOCKED_NO_SUPPLIER)

    if gov.get("state") in {None, GOV_VALUE_UNKNOWN} or not max_buy.get("supplier_quote_target"):
        blockers.append(QUOTE_BLOCKED_GOV_VALUE)

    if not original.get("original_source_verified") and not original.get("original_posting_url"):
        blockers.append(QUOTE_BLOCKED_SOLICITATION_DOCUMENTS)

    # Deduplicate blockers
    seen = set()
    uniq = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    blockers = uniq

    ready = len(blockers) == 0 and bool(tiers.get("quote_dependent_positive") or max_buy.get("supplier_quote_target"))
    # Ready requires quote-dependent positive economics OR at least max-buy + suppliers + gov
    if ready and not tiers.get("quote_dependent_positive"):
        # Allow readiness if max-buy + gov + suppliers even if qdep missed edge case
        ready = bool(
            max_buy.get("supplier_quote_target")
            and gov.get("state") in {GOV_VALUE_EXACT, GOV_VALUE_STRONG, GOV_VALUE_RANGE, GOV_VALUE_COMPARABLE}
            and suppliers
            and not blockers
        )

    submission_note = None
    if not (
        submission.get("submission_path_ready")
        or submission.get("submission_path_resolved")
        or submission.get("method")
        or (submission.get("checks") or {}).get("submission_method_known")
    ):
        submission_note = SUBMISSION_PATH_REQUIRES_CONFIRMATION

    strong_lead = bool(ev.get("apparently_within_quote_target") or ev.get("public_price_within_target"))
    lead_status = STRONG_LEAD_WITHIN_TARGET if strong_lead else None

    status = READY_FOR_QUOTE_OUTREACH if ready else (blockers[0] if blockers else QUOTE_BLOCKED_OTHER)
    owner_gate = OWNER_APPROVAL_REQUIRED if ready else None

    internal = build_internal_quote_control(ev)
    packets = [
        build_supplier_facing_packet(
            row=row,
            commercial=commercial,
            requirement=requirement,
            freight_info=freight_info,
            supplier=s,
            original=original,
            uom=uom,
            internal_deadline_days=deadline_days,
        )
        for s in suppliers[:5]
    ]

    priority = quote_outreach_priority_score(
        ev={**ev, "suppliers": suppliers, "supplier_count": len(suppliers)},
        runway=runway,
        eligibility_ok=not any(
            b in {QUOTE_BLOCKED_ELIGIBILITY, QUOTE_BLOCKED_SOURCE_APPROVAL} for b in blockers
        ),
        recurring=recurring,
        strong_lead=strong_lead,
        original_complete=bool(original.get("original_source_verified")),
    )

    positive_origin = None
    if tiers.get("quote_dependent_positive"):
        positive_origin = L6_EXISTING_POSITIVE if was_l6_positive else L7_NEWLY_RECOVERED_POSITIVE

    # Profit tier label for READY rows
    profit_tier = None
    if tiers.get("ge_50k"):
        profit_tier = "ge_50k"
    elif tiers.get("ge_25k"):
        profit_tier = "ge_25k"
    elif tiers.get("ge_10k"):
        profit_tier = "ge_10k"
    elif tiers.get("ge_5k"):
        profit_tier = "ge_5k"
    elif tiers.get("quote_dependent_positive"):
        profit_tier = "positive"

    # ge_100k heuristic
    rev_mid = _f((ev.get("expected_revenue") or {}).get("ExpectedRevenueMid"))
    if rev_mid and rev_mid >= 150000 and tiers.get("ge_50k"):
        th = (max_buy.get("thresholds") or {})
        if _f(th.get("MAX_BUY_FOR_25K_PROFIT")) and _f(th.get("MAX_BUY_FOR_25K_PROFIT")) > 0:
            # rough: if revenue supports 100k after costs
            target = _f(max_buy.get("supplier_quote_target")) or 0
            if rev_mid - target - (rev_mid * 0.05) >= 100000:
                profit_tier = "ge_100k"
                tiers = {**tiers, "ge_100k": True}

    return {
        "status": status,
        "ready": ready,
        "blockers": blockers,
        "requirement": requirement,
        "freight_info": freight_info,
        "suppliers_ranked": suppliers,
        "supplier_packets": packets,
        "internal_quote_control": internal,
        "quote_runway": runway,
        "owner_gate": owner_gate,
        "submission_note": submission_note,
        "lead_status": lead_status,
        "quote_outreach_priority": priority,
        "positive_origin": positive_origin,
        "profit_tier": profit_tier,
        "recurring_signal": RECURRING_QUOTE_OPPORTUNITY if recurring else None,
        "send_authorized": False,
        "outreach_authorized": False,
        "approved_for_outreach": False,
    }


def owner_queue_row(
    row: dict[str, Any],
    *,
    ev: dict[str, Any],
    readiness: dict[str, Any],
    lane: str | None,
    original: dict[str, Any] | None,
    deadline_days: float | None,
) -> dict[str, Any]:
    gov = ev.get("government_value") or {}
    th = ((ev.get("max_buy") or {}).get("thresholds") or {})
    suppliers = readiness.get("suppliers_ranked") or []
    return {
        "priority": (readiness.get("quote_outreach_priority") or {}).get("score"),
        "opportunity": (row.get("title") or "")[:120],
        "opportunity_id": row.get("notice_id") or row.get("solicitation_id") or row.get("id"),
        "buyer": row.get("agency"),
        "deadline": (original or {}).get("authoritative_deadline") or row.get("deadline") or row.get("response_deadline"),
        "days_remaining": deadline_days,
        "product": (ev.get("quote_packet") or {}).get("product")
        or row.get("title"),
        "quantity": (ev.get("uom") or {}).get("quantity") or row.get("quantity"),
        "acquisition_lane": lane,
        "government_value": gov.get("unit_value") or gov.get("total_value"),
        "government_evidence_quality": gov.get("state"),
        "max_buy_5k": th.get("MAX_BUY_FOR_5K_PROFIT"),
        "max_buy_10k": th.get("MAX_BUY_FOR_10K_PROFIT"),
        "max_buy_25k": th.get("MAX_BUY_FOR_25K_PROFIT"),
        "expected_profit_tier": readiness.get("profit_tier"),
        "supplier_count": len(suppliers),
        "best_supplier_candidates": [
            {"name": s.get("name") or s.get("supplier_domain"), "auth": s.get("authorization_state"), "rank": s.get("rank")}
            for s in suppliers[:3]
        ],
        "freight_status": (readiness.get("freight_info") or {}).get("freight_class"),
        "eligibility_status": "OK"
        if not any(
            b in readiness.get("blockers") or []
            for b in (QUOTE_BLOCKED_ELIGIBILITY, QUOTE_BLOCKED_SOURCE_APPROVAL)
        )
        else "BLOCKED",
        "quote_readiness_blockers": readiness.get("blockers"),
        "original_solicitation_link": (original or {}).get("original_posting_url") or row.get("detail_url"),
        "status": readiness.get("status"),
        "owner_gate": readiness.get("owner_gate"),
        "positive_origin": readiness.get("positive_origin"),
    }


def filter_owner_queue(
    rows: list[dict[str, Any]],
    *,
    status: str | None = None,
    profit_tier: str | None = None,
    lane: str | None = None,
    blocked_only: bool = False,
    ready_only: bool = False,
    min_suppliers: int | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if ready_only and r.get("status") != READY_FOR_QUOTE_OUTREACH:
            continue
        if blocked_only and r.get("status") == READY_FOR_QUOTE_OUTREACH:
            continue
        if status and r.get("status") != status:
            continue
        if profit_tier and r.get("expected_profit_tier") != profit_tier:
            continue
        if lane and r.get("acquisition_lane") != lane:
            continue
        if min_suppliers is not None and int(r.get("supplier_count") or 0) < min_suppliers:
            continue
        out.append(r)
    return out


def remember_product(
    memory: dict[str, Any],
    *,
    key: str | None,
    payload: dict[str, Any],
) -> None:
    if not key:
        return
    memory.setdefault("products", {})
    memory["products"][str(key)[:120]] = {**payload, "updated_at": _utc()}
    memory["updated_at"] = _utc()
