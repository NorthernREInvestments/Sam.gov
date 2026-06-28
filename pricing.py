"""Pricing intelligence service — Tier 1 regional benchmarks + Tier 2 internal database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from internal_pricing import build_pricing_dashboard, query_internal_pricing
from pws_fields import pws_snapshot
from usaspending_client import extract_work_location, fetch_regional_benchmarks


def get_regional_benchmark(contract: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    """Tier 1 — USAspending regional award benchmarks (cached on contract.pricing_intel)."""
    naics_code = (contract.naics_code or "").strip() or None
    work_location = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
    )
    state_code = work_location.get("state_code")

    cached = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else None
    if cached and not force_refresh and _cache_fresh(cached) and cached.get("tier") == "regional_benchmark":
        return cached

    if not naics_code:
        return _error_payload("NAICS code missing — cannot look up regional benchmarks.", naics_code, state_code)
    if not state_code:
        return _error_payload(
            "Could not determine where the work is performed — need a state for regional pricing lookup.",
            naics_code,
            state_code,
        )

    try:
        intel = fetch_regional_benchmarks(naics_code, state_code)
    except Exception as exc:
        return _error_payload(f"USAspending lookup failed: {exc}", naics_code, state_code)

    intel["cached_at"] = datetime.now(timezone.utc).isoformat()
    intel["tier"] = "regional_benchmark"
    contract.pricing_intel = intel
    return intel


def get_full_pricing_intel(contract: Any, session, *, force_refresh: bool = False) -> dict[str, Any]:
    """Combined pricing payload for contract detail UI."""
    regional = get_regional_benchmark(contract, force_refresh=force_refresh)
    internal = query_internal_pricing(session, contract)
    pws = pws_snapshot(contract)

    incumbent = regional.get("likely_incumbent") or regional.get("most_frequent_winner")
    competitive = {
        "most_frequent_winner": regional.get("most_frequent_winner"),
        "most_frequent_winner_count": regional.get("most_frequent_winner_count"),
        "incumbent": incumbent,
        "incumbent_note": (
            "This company may be the incumbent. Price competitively to displace them."
            if incumbent
            else None
        ),
    }

    payload = {
        "pws": pws,
        "internal": internal,
        "regional_benchmark": regional,
        "competitive": competitive,
        "selected_sub_quote": float(contract.selected_sub_quote) if contract.selected_sub_quote else None,
        "status": contract.status,
        "awarded_amount": float(contract.awarded_amount) if contract.awarded_amount else None,
        "notice_id": contract.notice_id,
        "cached_at": regional.get("cached_at"),
    }
    if isinstance(contract.pricing_intel, dict):
        contract.pricing_intel["internal_snapshot"] = internal
    return payload


def get_contract_pricing_intel(contract: Any, *, force_refresh: bool = False, session=None) -> dict[str, Any]:
    """Backward-compatible entry — returns full payload when session provided."""
    if session is not None:
        return get_full_pricing_intel(contract, session, force_refresh=force_refresh)
    return get_regional_benchmark(contract, force_refresh=force_refresh)


def get_pricing_dashboard(session) -> dict[str, Any]:
    return build_pricing_dashboard(session)


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
        "tier": "regional_benchmark",
        "source": "USAspending.gov",
    }
