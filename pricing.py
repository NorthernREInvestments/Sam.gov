"""Pricing intelligence service — Tier 1 regional benchmarks + Tier 2 internal database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from internal_pricing import build_pricing_dashboard, query_internal_pricing
from pws_fields import pws_snapshot
from usaspending_client import (
    DEFAULT_LOOKBACK_YEARS,
    contract_number_search_variants,
    extract_contract_numbers,
    extract_facility_search_terms,
    extract_pricing_hints_from_text,
    extract_work_location,
    fetch_predecessor_pricing,
    fetch_regional_benchmarks,
    normalize_contract_number,
)


def _solicitation_pricing_hints(contract: Any) -> dict[str, Any]:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    manual = (sol.get("manual_previous_contract_number") or "").strip() or None
    previous = manual or (sol.get("previous_contract_number") or "").strip() or None
    incumbent = (sol.get("incumbent_contractor") or "").strip() or None
    extra_numbers: list[str] = []
    exclude_numbers: list[str] = []
    for current in (
        sol.get("solicitation_number"),
        getattr(contract, "notice_id", None),
    ):
        for variant in contract_number_search_variants(current):
            if variant and variant not in exclude_numbers:
                exclude_numbers.append(variant)
    facility_terms = extract_facility_search_terms(
        getattr(contract, "title", None),
        contract.description or analysis.get("plain_english_summary"),
        location=getattr(contract, "location", None),
        agency=getattr(contract, "agency", None),
    )

    for blob in (
        getattr(contract, "attachment_text", None),
        contract.description,
        analysis.get("plain_english_summary"),
    ):
        if not blob:
            continue
        text_hints = extract_pricing_hints_from_text(str(blob))
        if not incumbent and text_hints.get("incumbent_contractor"):
            incumbent = text_hints["incumbent_contractor"]
        if not previous and text_hints.get("previous_contract_number"):
            previous = text_hints["previous_contract_number"]
        for number in text_hints.get("extra_contract_numbers") or extract_contract_numbers(str(blob)):
            normalized = normalize_contract_number(number)
            if normalized and normalized not in extra_numbers:
                extra_numbers.append(normalized)

    if previous:
        primary = normalize_contract_number(previous)
        ordered = [n for n in extra_numbers if n != primary]
        extra_numbers = ([primary] if primary else []) + ordered
    elif extra_numbers:
        previous = extra_numbers[0]

    exclude_set = set(exclude_numbers)
    extra_numbers = [n for n in extra_numbers if n not in exclude_set]

    return {
        "previous_contract_number": previous,
        "incumbent_contractor": incumbent,
        "extra_contract_numbers": extra_numbers,
        "exclude_contract_numbers": exclude_numbers,
        "facility_terms": facility_terms,
        "manual_lookup": bool(manual),
    }


def _merge_predecessor(intel: dict[str, Any], predecessor: dict[str, Any] | None) -> dict[str, Any]:
    if not predecessor or not predecessor.get("is_prior_contract"):
        return intel
    intel = dict(intel)
    intel["predecessor_award"] = predecessor
    intel["prior_contract_annual"] = predecessor.get("annual_amount")
    intel["prior_contract_total"] = predecessor.get("total_value")
    intel["likely_incumbent"] = predecessor.get("recipient_name") or intel.get("likely_incumbent")
    intel["lookup_method"] = predecessor.get("lookup_method")
    intel["previous_contract_number"] = predecessor.get("contract_number")
    if predecessor.get("option_years_exercised") is not None:
        intel["option_years_exercised"] = predecessor.get("option_years_exercised")
    return intel


def get_regional_benchmark(contract: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    """Tier 1 — USAspending regional award benchmarks (cached on contract.pricing_intel)."""
    naics_code = (contract.naics_code or "").strip() or None
    work_location = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
        title=getattr(contract, "title", None),
        description=getattr(contract, "description", None),
    )
    state_code = work_location.get("state_code")
    city = work_location.get("city")
    hints = _solicitation_pricing_hints(contract)

    cached = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else None
    cache_key = (
        hints.get("previous_contract_number"),
        hints.get("incumbent_contractor"),
        tuple(hints.get("extra_contract_numbers") or ()),
        tuple(hints.get("facility_terms") or ()),
        hints.get("manual_lookup"),
    )
    if (
        cached
        and not force_refresh
        and _cache_fresh(cached)
        and cached.get("tier") == "regional_benchmark"
        and cached.get("pricing_hints") == cache_key
    ):
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
        from location_matching import extract_site_profile, extract_site_profiles

        site_profiles = extract_site_profiles(contract)
        origin_profile = dict(site_profiles[0] if site_profiles else extract_site_profile(contract))
        if site_profiles:
            origin_profile["_all_profiles"] = site_profiles
        origin_profile["_exclude_contract_numbers"] = hints.get("exclude_contract_numbers") or []

        intel = fetch_regional_benchmarks(
            naics_code,
            state_code,
            origin_profile=origin_profile,
            agency=contract.agency,
            city=city,
        )
        predecessor = fetch_predecessor_pricing(
            previous_contract_number=hints.get("previous_contract_number"),
            incumbent_contractor=hints.get("incumbent_contractor"),
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=contract.agency,
            extra_contract_numbers=hints.get("extra_contract_numbers"),
            origin_profile=origin_profile,
            facility_terms=hints.get("facility_terms"),
            manual_lookup=bool(hints.get("manual_lookup")),
            title=getattr(contract, "title", None),
        )
        intel = _merge_predecessor(intel, predecessor)
    except Exception as exc:
        payload = _error_payload(f"USAspending lookup failed: {exc}", naics_code, state_code)
        if cached and cached.get("average_annual_award") and not force_refresh:
            return cached
        if cached and cached.get("average_annual_award"):
            payload["stale_cache"] = True
            payload["average_annual_award"] = cached.get("average_annual_award")
            payload["awards_count"] = cached.get("awards_count")
            payload["state_code"] = cached.get("state_code") or state_code
            payload["naics_code"] = cached.get("naics_code") or naics_code
        contract.pricing_intel = payload
        return payload

    intel["cached_at"] = datetime.now(timezone.utc).isoformat()
    intel["tier"] = "regional_benchmark"
    intel["pricing_hints"] = cache_key
    intel["lookback_years"] = DEFAULT_LOOKBACK_YEARS
    contract.pricing_intel = intel
    return intel


def get_full_pricing_intel(contract: Any, session, *, force_refresh: bool = False) -> dict[str, Any]:
    """Combined pricing payload for contract detail UI."""
    from location_matching import query_site_contract_history

    regional = get_regional_benchmark(contract, force_refresh=force_refresh)
    internal = query_internal_pricing(session, contract)
    site_history = query_site_contract_history(session, contract)
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

    from proposal_defaults import resolve_contract_margin

    margin = resolve_contract_margin(contract)

    payload = {
        "pws": pws,
        "internal": internal,
        "regional_benchmark": regional,
        "site_history": site_history,
        "competitive": competitive,
        "selected_sub_quote": float(contract.selected_sub_quote) if contract.selected_sub_quote else None,
        "margin_percentage": float(contract.margin_percentage) if contract.margin_percentage is not None else None,
        "effective_margin_pct": margin,
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


def lookup_prior_contract_by_number(contract: Any, contract_number: str) -> dict[str, Any]:
    """Manual prior-contract lookup — USAspending only, no SAM.gov calls."""
    number = (contract_number or "").strip()
    if not number:
        raise ValueError("Contract number is required.")

    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    sol = dict(analysis.get("solicitation_meta") or {}) if isinstance(analysis.get("solicitation_meta"), dict) else {}
    sol["manual_previous_contract_number"] = number
    analysis["solicitation_meta"] = sol
    contract.analysis = analysis
    contract.pricing_intel = None
    return get_regional_benchmark(contract, force_refresh=True)


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


def contract_missing_prior_dollars(contract: Any) -> bool:
    """True when a scored contract has a work state but no dollar line for the dashboard card."""
    from watchlist_pricing import contract_has_watchlist_pricing

    if contract_has_watchlist_pricing(contract):
        return False
    from display_format import pricing_card_display, prior_hints_from_contract

    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    if analysis.get("score") is None and analysis.get("text_score") is None:
        return False
    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
        title=getattr(contract, "title", None),
        description=getattr(contract, "description", None),
    )
    if not work.get("state_code"):
        return False
    hints = prior_hints_from_contract(contract)
    intel = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else {}
    card = pricing_card_display(intel, has_work_state=True, prior_hints=hints)
    return card.get("kind") != "prior_contract"


def contract_pricing_needs_refresh(contract: Any) -> bool:
    """True when we should re-query USAspending for prior-contract dollars."""
    from watchlist_pricing import contract_has_watchlist_pricing

    if contract_has_watchlist_pricing(contract):
        return False
    if contract_missing_prior_dollars(contract):
        return True
    hints = _solicitation_pricing_hints(contract)
    has_hint = bool(hints.get("previous_contract_number") or hints.get("incumbent_contractor"))
    intel = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else {}
    pred = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
    has_amount = bool(
        pred.get("recent_annual_amount")
        or pred.get("annual_amount")
        or pred.get("total_value")
        or intel.get("average_annual_award")
        or (isinstance(intel.get("awards"), list) and intel.get("awards"))
    )
    if has_hint and not has_amount:
        return True
    if hints.get("previous_contract_number") and intel.get("awards_count", 0) == 0:
        work = extract_work_location(
            contract.location,
            contract.sam_raw if isinstance(contract.sam_raw, dict) else None,
            title=getattr(contract, "title", None),
            description=getattr(contract, "description", None),
        )
        return bool(work.get("state_code"))
    return False
