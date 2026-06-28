"""Pricing intelligence service for contract detail views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from usaspending_client import extract_work_location, fetch_pricing_intelligence


def get_contract_pricing_intel(contract: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    """Return cached or freshly fetched USAspending pricing intelligence."""
    naics_code = (contract.naics_code or "").strip() or None
    work_location = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
    )
    state_code = work_location.get("state_code")
    city = work_location.get("city")
    zip_code = work_location.get("zip")

    cached = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else None
    if cached and not force_refresh and _cache_fresh(cached) and _cache_has_distances(cached):
        return cached

    if not naics_code:
        return _error_payload("NAICS code missing — cannot look up comparable awards.", naics_code, state_code)

    if not state_code:
        return _error_payload(
            "Could not determine where the work is performed — need a state for regional pricing lookup.",
            naics_code,
            state_code,
        )

    try:
        intel = fetch_pricing_intelligence(
            naics_code,
            state_code,
            city=city,
            zip_code=zip_code,
            origin_location=work_location,
        )
    except Exception as exc:
        return _error_payload(f"USAspending lookup failed: {exc}", naics_code, state_code)

    intel["cached_at"] = datetime.now(timezone.utc).isoformat()
    contract.pricing_intel = intel
    return intel


def _cache_has_distances(payload: dict[str, Any]) -> bool:
    if not payload.get("location_scope"):
        return False
    awards = payload.get("awards") or []
    if not awards:
        return True
    return "distance_label" in awards[0]


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
