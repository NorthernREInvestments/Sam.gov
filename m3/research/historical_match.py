"""Historical product match classes + award evidence normalization.

On-demand research only — not a historical award warehouse.
OpenAI (or a later deterministic adapter) must return match_class + match_reason
so bidding can trust whether a historical price applies to the current product.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical match classes (owner-facing + bidding later)
MATCH_EXACT_NSN = "EXACT_NSN_MATCH"
MATCH_EXACT_MPN = "EXACT_MANUFACTURER_PART_NUMBER_MATCH"
MATCH_EXACT_ID = "EXACT_VERIFIED_PRODUCT_IDENTIFIER_MATCH"
MATCH_BRAND_OR_EQUAL = "VERIFIED_BRAND_OR_EQUAL_MATCH"
MATCH_SPEC = "VERIFIED_SPECIFICATION_MATCH"
MATCH_MARKET = "GENERAL_MARKET_CONTEXT_ONLY"
MATCH_NONE = "NO_RELIABLE_MATCH"

CANONICAL_MATCH_CLASSES = frozenset(
    {
        MATCH_EXACT_NSN,
        MATCH_EXACT_MPN,
        MATCH_EXACT_ID,
        MATCH_BRAND_OR_EQUAL,
        MATCH_SPEC,
        MATCH_MARKET,
        MATCH_NONE,
    }
)

# Pricing evidence types — not interchangeable
PRICING_AWARDED_UNIT = "awarded_unit_price"
PRICING_AWARDED_EXTENDED = "awarded_extended_price"
PRICING_COMPETITOR_BID = "unsuccessful_competitor_bid"
PRICING_CATALOG_CONTRACT = "contract_authorized_catalog_price"
PRICING_PO = "purchase_order_price"
PRICING_EXPENDITURE = "government_expenditure"
PRICING_CONTRACT_TOTAL = "total_contract_award_amount"
PRICING_ESTIMATE = "estimated_contract_value"
PRICING_CEILING = "contract_ceiling"
PRICING_GOV_ESTIMATE = "government_estimate"
PRICING_INTENT = "intent_to_award"
PRICING_UNKNOWN = "unknown_pricing_type"

# Relevance of a historical price to the active opportunity
RELEVANCE_VERIFIED = "VERIFIED_COMPARABLE"
RELEVANCE_QUALIFIED = "QUALIFIED_COMPARABLE"
RELEVANCE_CONTEXT = "MARKET_CONTEXT_ONLY"
RELEVANCE_UNUSABLE = "UNUSABLE"

_LEGACY_MATCH = {
    "exact": MATCH_EXACT_MPN,
    "exact_mpn": MATCH_EXACT_MPN,
    "exact_nsn": MATCH_EXACT_NSN,
    "approximate": MATCH_MARKET,
    "approx": MATCH_MARKET,
    "similar": MATCH_MARKET,
    "brand_or_equal": MATCH_BRAND_OR_EQUAL,
    "specification": MATCH_SPEC,
    "spec": MATCH_SPEC,
    "none": MATCH_NONE,
    "no_match": MATCH_NONE,
    "unrelated": MATCH_NONE,
}


def normalize_match_class(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return MATCH_NONE
    up = s.upper().replace(" ", "_").replace("-", "_")
    if up in CANONICAL_MATCH_CLASSES:
        return up
    # Allow shortened forms already canonical-ish
    for c in CANONICAL_MATCH_CLASSES:
        if up == c or up in c or c.endswith(up):
            if up in {c, c.replace("_MATCH", ""), c.replace("EXACT_", "").replace("_MATCH", "")}:
                return c
    low = s.lower().strip()
    if low in _LEGACY_MATCH:
        return _LEGACY_MATCH[low]
    if "nsn" in low and "exact" in low:
        return MATCH_EXACT_NSN
    if ("part" in low or "mpn" in low) and "exact" in low:
        return MATCH_EXACT_MPN
    if "brand" in low or "or equal" in low or "or-equal" in low:
        return MATCH_BRAND_OR_EQUAL
    if "spec" in low:
        return MATCH_SPEC
    if "market" in low or "context" in low or "approximate" in low:
        return MATCH_MARKET
    return MATCH_NONE


def normalize_pricing_type(raw: str | None) -> str:
    s = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "unit": PRICING_AWARDED_UNIT,
        "unit_price": PRICING_AWARDED_UNIT,
        "awarded_unit": PRICING_AWARDED_UNIT,
        "awarded_unit_price": PRICING_AWARDED_UNIT,
        "extended": PRICING_AWARDED_EXTENDED,
        "extended_price": PRICING_AWARDED_EXTENDED,
        "competitor": PRICING_COMPETITOR_BID,
        "bid": PRICING_COMPETITOR_BID,
        "catalog": PRICING_CATALOG_CONTRACT,
        "po": PRICING_PO,
        "purchase_order": PRICING_PO,
        "expenditure": PRICING_EXPENDITURE,
        "total": PRICING_CONTRACT_TOTAL,
        "contract_total": PRICING_CONTRACT_TOTAL,
        "award_total": PRICING_CONTRACT_TOTAL,
        "estimate": PRICING_ESTIMATE,
        "estimated": PRICING_ESTIMATE,
        "ceiling": PRICING_CEILING,
        "gov_estimate": PRICING_GOV_ESTIMATE,
        "intent": PRICING_INTENT,
    }
    if s in aliases:
        return aliases[s]
    known = {
        PRICING_AWARDED_UNIT,
        PRICING_AWARDED_EXTENDED,
        PRICING_COMPETITOR_BID,
        PRICING_CATALOG_CONTRACT,
        PRICING_PO,
        PRICING_EXPENDITURE,
        PRICING_CONTRACT_TOTAL,
        PRICING_ESTIMATE,
        PRICING_CEILING,
        PRICING_GOV_ESTIMATE,
        PRICING_INTENT,
        PRICING_UNKNOWN,
    }
    return s if s in known else PRICING_UNKNOWN


def relevance_for_match(match_class: str, pricing_type: str) -> str:
    """Deterministic floor: weak match or non-unit pricing cannot be VERIFIED_COMPARABLE."""
    mc = normalize_match_class(match_class)
    pt = normalize_pricing_type(pricing_type)
    if mc == MATCH_NONE:
        return RELEVANCE_UNUSABLE
    if pt in {
        PRICING_CONTRACT_TOTAL,
        PRICING_CEILING,
        PRICING_ESTIMATE,
        PRICING_GOV_ESTIMATE,
        PRICING_INTENT,
        PRICING_UNKNOWN,
    }:
        return RELEVANCE_UNUSABLE if mc in {MATCH_MARKET, MATCH_NONE} else RELEVANCE_CONTEXT
    if mc == MATCH_MARKET:
        return RELEVANCE_CONTEXT
    if mc in {MATCH_EXACT_NSN, MATCH_EXACT_MPN, MATCH_EXACT_ID} and pt == PRICING_AWARDED_UNIT:
        return RELEVANCE_VERIFIED
    if mc in {MATCH_BRAND_OR_EQUAL, MATCH_SPEC} and pt in {
        PRICING_AWARDED_UNIT,
        PRICING_PO,
        PRICING_AWARDED_EXTENDED,
    }:
        return RELEVANCE_QUALIFIED
    if pt == PRICING_AWARDED_UNIT:
        return RELEVANCE_QUALIFIED
    return RELEVANCE_CONTEXT


def _norm_id(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def classify_identifier_match(
    *,
    current_nsn: str | None = None,
    current_mpn: str | None = None,
    historical_nsn: str | None = None,
    historical_mpn: str | None = None,
) -> tuple[str, str]:
    """Deterministic identifier comparison when both sides have IDs."""
    c_nsn, h_nsn = _norm_id(current_nsn), _norm_id(historical_nsn)
    if c_nsn and h_nsn:
        if c_nsn == h_nsn:
            return MATCH_EXACT_NSN, f"Exact NSN match: {current_nsn} equals historical {historical_nsn}."
        return (
            MATCH_NONE,
            f"NSNs differ (current {current_nsn} vs historical {historical_nsn}); not interchangeable.",
        )
    c_mpn, h_mpn = _norm_id(current_mpn), _norm_id(historical_mpn)
    if c_mpn and h_mpn:
        if c_mpn == h_mpn:
            return MATCH_EXACT_MPN, f"Exact manufacturer part number match: {current_mpn}."
        # Similar but not identical (suffix / revision differences)
        if c_mpn in h_mpn or h_mpn in c_mpn or c_mpn[: min(6, len(c_mpn))] == h_mpn[: min(6, len(h_mpn))]:
            return (
                MATCH_NONE,
                f"Part numbers are similar but not identical (current {current_mpn} vs historical "
                f"{historical_mpn}); do not treat as exact match.",
            )
        return (
            MATCH_NONE,
            f"Part numbers differ (current {current_mpn} vs historical {historical_mpn}).",
        )
    return MATCH_NONE, "Insufficient identifiers to classify an exact match."


def packaging_conversion_supported(
    *,
    historical_unit: str | None,
    historical_pack_qty: float | None,
    target_unit: str | None,
) -> tuple[bool, str]:
    """Never invent pack sizes. Only allow conversion when pack qty is explicit."""
    hu = (historical_unit or "").strip().lower()
    tu = (target_unit or "").strip().lower()
    if not hu or not tu:
        return False, "Unit of measure missing; conversion incomplete."
    if hu == tu or {hu, tu} <= {"ea", "each", "unit", "units"}:
        return True, "Units already aligned (each/unit)."
    if hu in {"case", "box", "pack", "carton"} and tu in {"ea", "each", "unit", "units"}:
        if historical_pack_qty and historical_pack_qty > 0:
            return True, f"Case/pack to each supported using verified pack qty {historical_pack_qty}."
        return False, "Case/pack price cannot convert to each without verified pack quantity."
    return False, f"Unsupported unit conversion ({historical_unit} → {target_unit})."


def normalize_award_record(
    raw: dict[str, Any] | None,
    *,
    current_nsn: str | None = None,
    current_mpn: str | None = None,
) -> dict[str, Any]:
    """Normalize one historical evidence row for UI + bidding handoff."""
    a = dict(raw or {})
    match_class = normalize_match_class(
        a.get("match_class") or a.get("match_type") or a.get("product_match_class")
    )
    match_reason = str(a.get("match_reason") or a.get("match_rationale") or "").strip()
    # Prefer deterministic ID classification when both sides have IDs
    hist_nsn = str(a.get("nsn") or a.get("national_stock_number") or "").strip() or None
    hist_mpn = str(
        a.get("part_number") or a.get("manufacturer_part_number") or a.get("mpn") or ""
    ).strip() or None
    if (current_nsn or current_mpn) and (hist_nsn or hist_mpn):
        det_class, det_reason = classify_identifier_match(
            current_nsn=current_nsn,
            current_mpn=current_mpn,
            historical_nsn=hist_nsn,
            historical_mpn=hist_mpn,
        )
        # Deterministic exact/none overrides model when IDs present
        if det_class in {MATCH_EXACT_NSN, MATCH_EXACT_MPN, MATCH_NONE}:
            match_class = det_class
            if not match_reason or det_class != normalize_match_class(a.get("match_class") or a.get("match_type")):
                match_reason = det_reason
    if not match_reason:
        match_reason = (
            "No match explanation returned; treat as NO_RELIABLE_MATCH until reviewed."
            if match_class == MATCH_NONE
            else "Match class present but explanation missing — review source evidence before bidding."
        )

    pricing_type = normalize_pricing_type(
        a.get("pricing_type") or a.get("price_type") or a.get("historical_pricing_type")
    )
    # Guard: contract totals must not masquerade as unit prices
    unit_price = a.get("unit_price")
    if pricing_type == PRICING_CONTRACT_TOTAL and unit_price not in (None, "", "null"):
        # Keep total; clear misleading unit usage flag
        a["unit_price_not_derived_from_contract_total"] = True
    if pricing_type in {PRICING_CONTRACT_TOTAL, PRICING_CEILING, PRICING_ESTIMATE} and a.get(
        "unit_price"
    ):
        # Do not invent unit from totals — drop unit_price for safety unless explicitly awarded_unit
        a["unit_price_suppressed"] = a.get("unit_price")
        a["unit_price"] = None
        a["notes"] = (
            str(a.get("notes") or "")
            + " Unit price suppressed because pricing_type is a contract total/ceiling/estimate."
        ).strip()

    relevance = str(a.get("relevance") or "").strip().upper() or relevance_for_match(
        match_class, pricing_type
    )
    if relevance not in {
        RELEVANCE_VERIFIED,
        RELEVANCE_QUALIFIED,
        RELEVANCE_CONTEXT,
        RELEVANCE_UNUSABLE,
    }:
        relevance = relevance_for_match(match_class, pricing_type)

    a["match_class"] = match_class
    a["match_type"] = match_class  # back-compat for existing UI/commercial
    a["match_reason"] = match_reason
    a["pricing_type"] = pricing_type
    a["relevance"] = relevance
    a["evidence_class"] = a.get("evidence_class") or "historical_benchmark"
    a["not_guaranteed_future_price"] = True
    if not a.get("unit_price") and pricing_type != PRICING_AWARDED_UNIT:
        a.setdefault("unit_price_display", "UNKNOWN")
    return a


def normalize_awards_list(
    awards: list[Any] | None,
    *,
    current_nsn: str | None = None,
    current_mpn: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in awards or []:
        if not isinstance(row, dict):
            continue
        out.append(
            normalize_award_record(row, current_nsn=current_nsn, current_mpn=current_mpn)
        )
    return out


HISTORICAL_AWARD_JSON_SCHEMA = (
    '{"awards":[{'
    '"buyer":"",'
    '"product":"",'
    '"part_number":"",'
    '"nsn":"",'
    '"match_class":"EXACT_NSN_MATCH|EXACT_MANUFACTURER_PART_NUMBER_MATCH|'
    "EXACT_VERIFIED_PRODUCT_IDENTIFIER_MATCH|VERIFIED_BRAND_OR_EQUAL_MATCH|"
    'VERIFIED_SPECIFICATION_MATCH|GENERAL_MARKET_CONTEXT_ONLY|NO_RELIABLE_MATCH",'
    '"match_reason":"1-3 sentences explaining why the historical product does or does not match",'
    '"pricing_type":"awarded_unit_price|awarded_extended_price|unsuccessful_competitor_bid|'
    "contract_authorized_catalog_price|purchase_order_price|government_expenditure|"
    'total_contract_award_amount|estimated_contract_value|contract_ceiling|government_estimate|intent_to_award|unknown_pricing_type",'
    '"award_date":"",'
    '"awardee":"",'
    '"quantity":"",'
    '"unit_of_measure":"",'
    '"pack_quantity_if_known":"",'
    '"unit_price":"",'
    '"total_award_value":"",'
    '"competing_bids":"",'
    '"bidder_count":"",'
    '"source_url":"",'
    '"notes":""'
    "}]}"
)


def historical_award_prompt(
    *,
    query: str,
    product: str | None = None,
    buyer: str | None = None,
    part_number: str | None = None,
    nsn: str | None = None,
) -> str:
    return (
        "Find official U.S. government historical award / bid tabulation / purchase order evidence "
        f"for: {query}. "
        + (f"Product: {product}. " if product else "")
        + (f"Buyer: {buyer}. " if buyer else "")
        + (f"Part number / MPN: {part_number}. " if part_number else "")
        + (f"NSN: {nsn}. " if nsn else "")
        + "Prefer bid tabs, council/board packets, award notices, and public contract documents. "
        "For EVERY award row you MUST set match_class AND match_reason. "
        "match_reason must explain the product comparison in plain language "
        "(same NSN/MPN/spec differences, packaging, configuration). "
        "Use NO_RELIABLE_MATCH when evidence is unrelated. "
        "Distinguish pricing_type carefully: never treat a multi-line contract total or ceiling "
        "as an awarded_unit_price. Do not invent unit prices by dividing award totals. "
        "If no reliable historical pricing exists, return {\"awards\":[]}. "
        "Cite source URLs. Return JSON only."
    )
