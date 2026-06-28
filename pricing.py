"""Pricing intelligence service for contract detail views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from usaspending_client import extract_state, fetch_pricing_intelligence


def get_contract_pricing_intel(contract: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    """Return cached or freshly fetched USAspending pricing intelligence."""
    naics_code = (contract.naics_code or "").strip() or None
    state_code = extract_state(contract.location, contract.sam_raw if isinstance(contract.sam_raw, dict) else None)

    cached = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else None
    if cached and not force_refresh and _cache_fresh(cached):
        return cached

    if not naics_code:
        return _error_payload("NAICS code missing — cannot look up comparable awards.", naics_code, state_code)

    if not state_code:
        return _error_payload(
            f"Could not determine state from location '{contract.location or 'unknown'}'.",
            naics_code,
            state_code,
        )

    try:
        intel = fetch_pricing_intelligence(naics_code, state_code)
    except Exception as exc:
        return _error_payload(f"USAspending lookup failed: {exc}", naics_code, state_code)

    intel["cached_at"] = datetime.now(timezone.utc).isoformat()
    contract.pricing_intel = intel
    return intel


def _cache_fresh(payload: dict[str, Any], max_age_days: int = 7) -> bool:
    stamp = payload.get("cached_at") or payload.get("fetched_at")
    if not stamp:
        return False
    try:
        cached_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - cached_at < timedelta(days=max_age_days)


def _error_payload(message: str, naics_code: str | None, state_code: str | None) -> dict[str, Any]:
    return {
        "error": message,
        "naics_code": naics_code,
        "state_code": state_code,
        "awards_count": 0,
        "source": "USAspending.gov",
    }
