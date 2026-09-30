"""Phase J — product history reconciliation (false-match safe)."""

from __future__ import annotations

import re
import statistics
from typing import Any

from phase_j.product_identity import (
    LEVEL_EXACT,
    LEVEL_STRONG,
    extract_mpns,
    extract_nsns,
    normalize_identifier,
    normalize_nsn,
)

BUILD_TAG = "20260925-m3-phase-j-history-1"

RECENCY_CURRENT = "CURRENT"  # 0-2y
RECENCY_RECENT = "RECENT"  # 2-5y
RECENCY_AGED = "AGED"  # 5-8y
RECENCY_STALE = "STALE"  # >8y

UNIT_PRICE_EXACT = "UNIT_PRICE_EXACT"
UNIT_PRICE_NORMALIZED = "UNIT_PRICE_NORMALIZED"
UNIT_PRICE_ESTIMATED = "UNIT_PRICE_ESTIMATED"
UNIT_PRICE_UNUSABLE = "UNIT_PRICE_UNUSABLE"

HIGHLY_COMPARABLE = "HIGHLY_COMPARABLE"
COMPARABLE = "COMPARABLE"
WEAK_COMPARISON = "WEAK_COMPARISON"
NOT_COMPARABLE = "NOT_COMPARABLE"

MATCH_CONFIRMED = "MATCH_CONFIRMED"
MATCH_STRONG = "MATCH_STRONG"
MATCH_PARTIAL = "MATCH_PARTIAL"
MATCH_REJECTED = "MATCH_REJECTED"
NO_MATCH = "NO_MATCH"

_QTY_RE = re.compile(
    r"\b(?:QTY|QUANTITY|QTY\.|QUAN)\s*[:#]?\s*([\d,]+(?:\.\d+)?)\b|"
    r"\b([\d,]+(?:\.\d+)?)\s*(?:EA|EACH)\b",
    re.I,
)
_UOM_RE = re.compile(
    r"\b(EA|EACH|PK|PACK|BX|BOX|KT|KIT|SE|SET|PR|PAIR|FT|FEET|LB|LBS|GAL|GALLON|LOT)\b",
    re.I,
)

_UOM_NORM = {
    "EA": "EA",
    "EACH": "EA",
    "PK": "PK",
    "PACK": "PK",
    "BX": "BX",
    "BOX": "BX",
    "KT": "KT",
    "KIT": "KT",
    "SE": "SE",
    "SET": "SE",
    "PR": "PR",
    "PAIR": "PR",
    "FT": "FT",
    "FEET": "FT",
    "LB": "LB",
    "LBS": "LB",
    "GAL": "GAL",
    "GALLON": "GAL",
    "LOT": "LOT",
}


def classify_recency(days_ago: float | None) -> str:
    if days_ago is None:
        return RECENCY_STALE
    d = float(days_ago)
    if d <= 730:
        return RECENCY_CURRENT
    if d <= 1825:
        return RECENCY_RECENT
    if d <= 2920:
        return RECENCY_AGED
    return RECENCY_STALE


def parse_qty_uom(description: str | None) -> dict[str, Any]:
    text = str(description or "")
    qty = None
    uom = None
    mq = _QTY_RE.search(text)
    if mq:
        try:
            raw = mq.group(1) or mq.group(2)
            qty = float(str(raw).replace(",", ""))
        except (ValueError, TypeError):
            qty = None
    mu = _UOM_RE.search(text)
    if mu:
        uom = _UOM_NORM.get(mu.group(1).upper())
    return {
        "quantity": qty,
        "uom": uom,
        "conversion": "exact" if qty is not None else "unknown",
    }


def award_amount(award: dict[str, Any]) -> float | None:
    for k in ("award_amount", "Amount", "total_obligation_amount", "total_obligated_amount", "unit_price"):
        try:
            if award.get(k) is not None and float(award.get(k)) > 0:
                return float(award.get(k))
        except (TypeError, ValueError):
            continue
    return None


def unit_price_from_award(award: dict[str, Any]) -> dict[str, Any]:
    total = award_amount(award)
    parsed = parse_qty_uom(str(award.get("description") or ""))
    qty = parsed.get("quantity")
    uom = parsed.get("uom")
    if award.get("unit_price") is not None:
        try:
            up = float(award["unit_price"])
            if up > 0:
                return {
                    "raw_total": total,
                    "raw_quantity": qty,
                    "raw_uom": uom,
                    "derived_unit_price": up,
                    "unit_price_confidence": UNIT_PRICE_EXACT,
                }
        except (TypeError, ValueError):
            pass
    if total is not None and qty and qty > 0:
        return {
            "raw_total": total,
            "raw_quantity": qty,
            "raw_uom": uom or "EA",
            "derived_unit_price": round(total / qty, 4),
            "unit_price_confidence": UNIT_PRICE_NORMALIZED,
            "note": "total/qty from award description QTY field",
        }
    if total is not None and qty is None:
        # Total alone — unusable as unit price; may still be lot revenue if qty==1 unknown
        return {
            "raw_total": total,
            "raw_quantity": None,
            "raw_uom": uom,
            "derived_unit_price": None,
            "unit_price_confidence": UNIT_PRICE_UNUSABLE,
            "lot_total_only": True,
        }
    return {
        "raw_total": total,
        "raw_quantity": qty,
        "raw_uom": uom,
        "derived_unit_price": None,
        "unit_price_confidence": UNIT_PRICE_UNUSABLE,
    }


def reconcile_award(
    current: dict[str, Any],
    award: dict[str, Any],
    *,
    solicitation_uom: str | None = None,
) -> dict[str, Any]:
    """Compare one historical award to current product identity."""
    desc = str(award.get("description") or "")
    award_nsns = extract_nsns(desc)
    award_mpns = extract_mpns(desc)
    cur_nsn = normalize_nsn(current.get("nsn"))
    cur_mpn = normalize_identifier(current.get("mpn") or current.get("normalized_mpn"))
    matched: list[str] = []
    conflicting: list[str] = []

    identity_ok = False
    match_result = NO_MATCH

    if cur_nsn:
        award_nsn_norms = [normalize_nsn(n) for n in award_nsns]
        if cur_nsn in award_nsn_norms or cur_nsn in desc or cur_nsn.replace("-", "") in re.sub(r"\D", "", desc):
            matched.append("nsn_exact")
            identity_ok = True
            match_result = MATCH_CONFIRMED
        elif award_nsns:
            conflicting.append(f"award_nsn_differs:{award_nsns[0]}")
            match_result = MATCH_REJECTED
        else:
            # Keyword hit without NSN in description — reject when current has NSN
            conflicting.append("current_has_nsn_award_desc_missing_nsn")
            match_result = MATCH_REJECTED
    elif cur_mpn:
        award_mpn_norms = [normalize_identifier(m) for m in award_mpns if normalize_identifier(m)]
        desc_norm = normalize_identifier(desc) or ""
        if cur_mpn in award_mpn_norms or cur_mpn in desc_norm:
            matched.append("mpn_exact")
            identity_ok = True
            match_result = MATCH_STRONG if not current.get("cage") else MATCH_CONFIRMED
        elif award_mpns:
            conflicting.append(f"award_mpn_differs:{award_mpns[0]}")
            match_result = MATCH_REJECTED
        else:
            conflicting.append("current_has_mpn_award_missing_mpn")
            match_result = MATCH_REJECTED
    else:
        # No current NSN/MPN — title-only history is never sufficient
        conflicting.append("no_current_identity_key")
        match_result = MATCH_REJECTED

    days = award.get("days_ago")
    try:
        days_f = float(days) if days is not None else None
    except (TypeError, ValueError):
        days_f = None
    recency = classify_recency(days_f)
    price = unit_price_from_award(award)
    parsed = parse_qty_uom(desc)

    uom_match = "unknown"
    if solicitation_uom and parsed.get("uom"):
        uom_match = "exact" if solicitation_uom.upper() == parsed["uom"] else "mismatch"
    elif parsed.get("uom"):
        uom_match = "award_only"

    # Comparability
    if match_result == MATCH_REJECTED or not identity_ok:
        comparability = NOT_COMPARABLE
    elif price["unit_price_confidence"] == UNIT_PRICE_UNUSABLE and not price.get("lot_total_only"):
        comparability = WEAK_COMPARISON
    elif recency == RECENCY_STALE:
        # Exact identity stale → weak reference only
        comparability = WEAK_COMPARISON
    elif match_result == MATCH_CONFIRMED and recency in {RECENCY_CURRENT, RECENCY_RECENT}:
        if price["unit_price_confidence"] in {UNIT_PRICE_EXACT, UNIT_PRICE_NORMALIZED} or price.get("lot_total_only"):
            comparability = HIGHLY_COMPARABLE
        else:
            comparability = COMPARABLE
    elif match_result in {MATCH_CONFIRMED, MATCH_STRONG} and recency == RECENCY_AGED:
        # Exact/strong + aged: comparable anchor, not current market certainty
        if price["unit_price_confidence"] in {UNIT_PRICE_EXACT, UNIT_PRICE_NORMALIZED} or price.get("lot_total_only"):
            comparability = COMPARABLE
        else:
            comparability = WEAK_COMPARISON
    elif match_result == MATCH_STRONG and recency in {RECENCY_CURRENT, RECENCY_RECENT}:
        comparability = COMPARABLE
    else:
        comparability = WEAK_COMPARISON

    drives_economics = comparability in {HIGHLY_COMPARABLE, COMPARABLE} and price[
        "unit_price_confidence"
    ] != UNIT_PRICE_ESTIMATED

    # Lot-total-only with exact NSN: usable as lot revenue basis (not unit price)
    if (
        identity_ok
        and match_result == MATCH_CONFIRMED
        and price.get("lot_total_only")
        and recency in {RECENCY_CURRENT, RECENCY_RECENT, RECENCY_AGED}
    ):
        drives_economics = True
        if comparability == NOT_COMPARABLE:
            comparability = COMPARABLE

    return {
        "kind": "ProductHistoryReconciliation",
        "build": BUILD_TAG,
        "current_identity": {
            "nsn": current.get("nsn"),
            "mpn": current.get("mpn"),
            "identity_level": current.get("identity_level"),
        },
        "candidate_history": {
            "award_id": award.get("award_id") or award.get("Award ID"),
            "award_date": award.get("award_date"),
            "days_ago": days_f,
            "awardee": award.get("recipient_name") or award.get("Recipient Name"),
            "description": desc[:240],
            "amount": price.get("raw_total"),
        },
        "matched_fields": matched,
        "conflicting_fields": conflicting,
        "uom_comparison": uom_match,
        "quantity_comparison": {
            "award_qty": parsed.get("quantity"),
            "award_uom": parsed.get("uom"),
            "solicitation_uom": solicitation_uom,
        },
        "confidence": match_result,
        "comparability": comparability,
        "unit_price": price,
        "unit_price_usability": price.get("unit_price_confidence"),
        "recency": recency,
        "drives_economics": drives_economics,
        "reference_only": recency == RECENCY_STALE or comparability == WEAK_COMPARISON,
        "evidence": [{"source": "usaspending", "award_id": award.get("award_id")}],
        "blockers": conflicting if not drives_economics else [],
        "final_reconciliation_result": match_result,
    }


def reconcile_awards(
    current: dict[str, Any],
    awards: list[dict[str, Any]],
    *,
    solicitation_uom: str | None = None,
) -> dict[str, Any]:
    results = [reconcile_award(current, a, solicitation_uom=solicitation_uom) for a in (awards or [])]
    usable = [r for r in results if r.get("drives_economics")]
    rejected = [r for r in results if r.get("final_reconciliation_result") == MATCH_REJECTED]
    weak = [r for r in results if r.get("comparability") == WEAK_COMPARISON]

    # History class for readiness
    highly = sum(1 for r in usable if r.get("comparability") == HIGHLY_COMPARABLE)
    comp = sum(1 for r in usable if r.get("comparability") == COMPARABLE)
    if highly >= 2 or (highly >= 1 and comp >= 1) or highly >= 1 and len(usable) >= 2:
        history_class = "STRONG_HISTORY"
    elif highly >= 1 or comp >= 1:
        history_class = "MODERATE_HISTORY"
    elif weak or results:
        history_class = "WEAK_HISTORY"
    else:
        history_class = "NO_HISTORY_FOUND"

    revenue, revenue_basis, price_stats = revenue_from_reconciliations(usable)

    return {
        "kind": "ProductHistoryReconciliationSet",
        "build": BUILD_TAG,
        "reconciliations": results,
        "usable_count": len(usable),
        "rejected_count": len(rejected),
        "weak_count": len(weak),
        "history_class": history_class,
        "revenue": revenue,
        "revenue_basis": revenue_basis,
        "price_stats": price_stats,
        "false_match_blocked": len(rejected),
    }


def revenue_from_reconciliations(
    usable: list[dict[str, Any]],
) -> tuple[float | None, str, dict[str, Any]]:
    if not usable:
        return None, "no_comparable_history", {}
    lot_totals: list[float] = []
    unit_prices: list[float] = []
    for r in usable:
        up = r.get("unit_price") or {}
        if up.get("raw_total") is not None:
            lot_totals.append(float(up["raw_total"]))
        if up.get("derived_unit_price") is not None and up.get("unit_price_confidence") in {
            UNIT_PRICE_EXACT,
            UNIT_PRICE_NORMALIZED,
        }:
            unit_prices.append(float(up["derived_unit_price"]))

    stats: dict[str, Any] = {
        "comparable_awards": len(usable),
        "unit_prices": unit_prices,
        "lot_totals": lot_totals,
    }
    if unit_prices:
        unit_prices_sorted = sorted(unit_prices)
        stats.update(
            {
                "unit_price_low": unit_prices_sorted[0],
                "unit_price_median": statistics.median(unit_prices_sorted),
                "unit_price_high": unit_prices_sorted[-1],
                "unit_price_most_recent": unit_prices[0],
            }
        )
        # Without solicitation qty, use most recent lot total if available else median unit * unknown
        if lot_totals:
            lot_sorted = sorted(lot_totals)
            mid = lot_sorted[len(lot_sorted) // 2]
            stats["lot_median"] = mid
            return mid, "comparable_lot_total_median", stats
        # Unit price alone cannot invent quantity — unusable for revenue
        return None, "unit_price_known_quantity_unknown", stats

    if lot_totals:
        lot_sorted = sorted(lot_totals)
        mid = lot_sorted[len(lot_sorted) // 2]
        stats["lot_median"] = mid
        # Prefer most recent among usable
        recent = lot_totals[0]
        stats["lot_most_recent"] = recent
        return mid, "comparable_lot_total_median_exact_nsn", stats

    return None, "no_usable_price", stats


def history_search_keywords(identity: dict[str, Any]) -> list[str]:
    """NSN-first; then MPN. Never bare noun-only keywords."""
    keys: list[str] = []
    nsn = identity.get("nsn") or identity.get("normalized_nsn")
    if nsn:
        keys.append(nsn)
        keys.append(str(nsn).replace("-", ""))
        return keys
    mpn = identity.get("mpn") or identity.get("normalized_mpn")
    if mpn:
        keys.append(str(mpn))
        norm = normalize_identifier(str(mpn))
        if norm and norm != mpn:
            keys.append(norm)
        return keys
    return []
