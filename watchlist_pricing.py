"""Apply GovSpend watchlist values — skip USAspending lookup on high-confidence matches."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def should_skip_usaspending_lookup(contract: Any) -> bool:
    """
    Skip USAspending when a watchlist fingerprint is High confidence,
    or a Possible match was manually confirmed.
    """
    from gs_watchlist_service import fingerprint_meta

    meta = fingerprint_meta(contract) or {}
    if meta.get("match_rejected"):
        return False
    confidence = meta.get("match_confidence")
    if confidence == "High":
        return True
    if confidence == "Possible" and meta.get("match_confirmed"):
        return True
    if meta.get("sam_found") and confidence not in ("Possible", "Weak", "None"):
        return True
    return False


def pricing_from_watchlist(contract: Any) -> dict[str, Any] | None:
    from gs_watchlist_service import fingerprint_meta, target_from_meta

    meta = fingerprint_meta(contract) or {}
    if not should_skip_usaspending_lookup(contract):
        return None

    target = target_from_meta(meta)
    estimated_annual = meta.get("estimated_annual_value") or meta.get("award_amount")
    if target and target.estimated_annual_value is not None:
        estimated_annual = target.estimated_annual_value
    award_amount = meta.get("award_amount")
    if target and target.award_amount is not None:
        award_amount = target.award_amount

    if estimated_annual is None and award_amount is None:
        return None

    try:
        contract_value = float(estimated_annual if estimated_annual is not None else award_amount)
        historical = float(award_amount) if award_amount is not None else contract_value
    except (TypeError, ValueError):
        return None
    if contract_value <= 0:
        return None

    incumbent = (
        meta.get("incumbent_name")
        or (target.incumbent_name if target else None)
        or "Incumbent"
    )
    contracting_office = (
        meta.get("contracting_office")
        or meta.get("agency")
        or (target.contracting_office if target else None)
        or (target.agency if target else None)
    )
    confidence = meta.get("match_confidence") or "High"
    now = datetime.now(timezone.utc).isoformat()

    predecessor = {
        "is_prior_contract": True,
        "recipient_name": incumbent,
        "annual_amount": round(contract_value, 2),
        "recent_annual_amount": round(contract_value, 2),
        "contract_value": round(contract_value, 2),
        "total_value": round(historical, 2),
        "historical_award_amount": round(historical, 2),
        "awarding_office": contracting_office,
        "lookup_method": "govspend_watchlist",
        "confidence": "high",
        "watchlist_match_confidence": confidence,
        "pricing_calc_note": (
            "Contract value, incumbent, and awarding office from GovSpend gs_watchlist "
            f"({confidence} fingerprint match) — USAspending lookup skipped."
        ),
    }

    return {
        "watchlist_sourced": True,
        "skip_usaspending": True,
        "govspend_watchlist_id": meta.get("matched_watchlist_id") or meta.get("watchlist_id"),
        "contract_value": round(contract_value, 2),
        "awarding_office": contracting_office,
        "likely_incumbent": incumbent,
        "predecessor_award": predecessor,
        "prior_contract_annual": round(contract_value, 2),
        "prior_contract_total": round(historical, 2),
        "lookup_method": "govspend_watchlist",
        "tier": "watchlist_fingerprint",
        "source": "GovSpend gs_watchlist",
        "cached_at": now,
        "fetched_at": now,
    }


def apply_watchlist_pricing(contract: Any) -> bool:
    from usaspending_savings import record_usaspending_skip

    payload = pricing_from_watchlist(contract)
    if not payload:
        return False
    contract.pricing_intel = payload
    record_usaspending_skip(contract)
    return True


def contract_has_watchlist_pricing(contract: Any) -> bool:
    if not should_skip_usaspending_lookup(contract):
        return False
    intel = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else {}
    return bool(intel.get("watchlist_sourced") or intel.get("skip_usaspending"))
