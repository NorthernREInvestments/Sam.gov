"""Apply GovSpend watchlist award amounts — skip USAspending lookup when matched."""

from __future__ import annotations

from typing import Any

from gs_watchlist_service import govspend_watchlist_meta


def pricing_from_watchlist(contract: Any) -> dict[str, Any] | None:
    meta = govspend_watchlist_meta(contract)
    if not meta:
        return None
    amount = meta.get("award_amount")
    if amount is None:
        return None
    try:
        annual = float(amount)
    except (TypeError, ValueError):
        return None
    if annual <= 0:
        return None

    incumbent = meta.get("incumbent_name") or "Incumbent"
    return {
        "watchlist_sourced": True,
        "govspend_watchlist_id": meta.get("watchlist_id"),
        "predecessor_award": {
            "is_prior_contract": True,
            "recipient_name": incumbent,
            "annual_amount": round(annual, 2),
            "recent_annual_amount": round(annual, 2),
            "total_value": round(annual, 2),
            "lookup_method": "govspend_watchlist",
            "confidence": "high",
            "pricing_calc_note": "Prior contract value from GovSpend watchlist (USAspending lookup skipped).",
        },
    }


def apply_watchlist_pricing(contract: Any) -> bool:
    payload = pricing_from_watchlist(contract)
    if not payload:
        return False
    contract.pricing_intel = payload
    return True


def contract_has_watchlist_pricing(contract: Any) -> bool:
    intel = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else {}
    return bool(intel.get("watchlist_sourced"))
