"""USAspending.gov historical award search for pricing intelligence."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import date, timedelta
from typing import Any

import httpx

from geo import annotate_award_distances

BASE_URL = "https://api.usaspending.gov"
SEARCH_PATH = "/api/v2/search/spending_by_award/"

# Definitive contract award types (excludes grants, loans, IDVs)
CONTRACT_AWARD_TYPE_CODES = ["A", "B", "C", "D"]

AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "End Date",
    "Awarding Agency",
    "Contract Award Type",
    "Description",
    "Place of Performance State Code",
    "Place of Performance City Name",
    "Place of Performance Zip5",
]

# Pull extra candidates, then keep only scope-similar awards.
SEARCH_FETCH_LIMIT = 100
COMPARABLE_DISPLAY_LIMIT = 20

STATE_NAME_TO_CODE: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    **{code: code for code in [
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
        "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    ]},
}

STATE_CODE_TO_NAME: dict[str, str] = {
    code: name.title()
    for name, code in STATE_NAME_TO_CODE.items()
    if len(name) > 2
}

MIN_LOCAL_COMPARABLE_AWARDS = 3

# Land-adjacent states only — widest geographic search allowed.
BORDERING_STATES: dict[str, list[str]] = {
    "AL": ["MS", "TN", "GA", "FL"],
    "AK": [],
    "AZ": ["CA", "NV", "UT", "CO", "NM"],
    "AR": ["MO", "TN", "MS", "LA", "TX", "OK"],
    "CA": ["OR", "NV", "AZ"],
    "CO": ["WY", "NE", "KS", "OK", "NM", "AZ", "UT"],
    "CT": ["NY", "MA", "RI"],
    "DC": ["MD", "VA"],
    "DE": ["MD", "PA", "NJ"],
    "FL": ["GA", "AL"],
    "GA": ["FL", "AL", "TN", "NC", "SC"],
    "HI": [],
    "ID": ["MT", "WY", "UT", "NV", "OR", "WA"],
    "IL": ["WI", "IA", "MO", "KY", "IN"],
    "IN": ["MI", "OH", "KY", "IL"],
    "IA": ["MN", "WI", "IL", "MO", "NE", "SD"],
    "KS": ["NE", "MO", "OK", "CO"],
    "KY": ["IL", "IN", "OH", "WV", "VA", "TN", "MO"],
    "LA": ["TX", "AR", "MS"],
    "ME": ["NH"],
    "MD": ["PA", "DE", "WV", "VA", "DC"],
    "MA": ["NH", "RI", "CT", "NY", "VT"],
    "MI": ["OH", "IN", "WI"],
    "MN": ["WI", "IA", "SD", "ND"],
    "MS": ["LA", "AR", "TN", "AL"],
    "MO": ["IA", "IL", "KY", "TN", "AR", "OK", "KS", "NE"],
    "MT": ["ND", "SD", "WY", "ID"],
    "NE": ["SD", "IA", "MO", "KS", "CO", "WY"],
    "NV": ["OR", "ID", "UT", "AZ", "CA"],
    "NH": ["ME", "MA", "VT"],
    "NJ": ["NY", "PA", "DE"],
    "NM": ["AZ", "UT", "CO", "OK", "TX"],
    "NY": ["VT", "MA", "CT", "NJ", "PA"],
    "NC": ["VA", "TN", "GA", "SC"],
    "ND": ["MN", "SD", "MT"],
    "OH": ["PA", "WV", "KY", "IN", "MI"],
    "OK": ["KS", "MO", "AR", "TX", "NM", "CO"],
    "OR": ["WA", "ID", "NV", "CA"],
    "PA": ["NY", "NJ", "DE", "MD", "WV", "OH"],
    "RI": ["MA", "CT"],
    "SC": ["NC", "GA"],
    "SD": ["ND", "MN", "IA", "NE", "WY", "MT"],
    "TN": ["KY", "VA", "NC", "GA", "AL", "MS", "AR", "MO"],
    "TX": ["NM", "OK", "AR", "LA"],
    "UT": ["ID", "WY", "CO", "NM", "AZ", "NV"],
    "VT": ["NY", "NH", "MA"],
    "VA": ["MD", "WV", "KY", "TN", "NC", "DC"],
    "WA": ["ID", "OR"],
    "WV": ["PA", "MD", "VA", "KY", "OH"],
    "WI": ["MI", "MN", "IA", "IL"],
    "WY": ["MT", "SD", "NE", "CO", "UT", "ID"],
}


def _state_names(codes: list[str]) -> str:
    return ", ".join(STATE_CODE_TO_NAME.get(code, code) for code in codes)


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.strip().upper())
    if not cleaned:
        return None
    if cleaned in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[cleaned]
    match = re.fullmatch(r"[A-Z]{2}", cleaned)
    if match and cleaned in STATE_NAME_TO_CODE:
        return cleaned
    return None


def _parse_sam_location_block(block: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    city = block.get("city")
    if isinstance(city, dict):
        city = city.get("name") or city.get("code")
    state = block.get("state") or block.get("stateCode") or block.get("state_code")
    if isinstance(state, dict):
        state = state.get("code") or state.get("name")
    zip_code = block.get("zip") or block.get("zipcode") or block.get("zipCode")
    state_code = normalize_state(str(state) if state else "")
    city_name = str(city).strip() if city else None
    zip_text = str(zip_code).strip()[:5] if zip_code else None
    return city_name or None, state_code, zip_text


def extract_work_location(
    location: str | None,
    sam_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Where the work is performed — place of performance only, not contracting office."""
    city: str | None = None
    state_code: str | None = None
    zip_code: str | None = None

    if sam_raw:
        work_states = sam_raw.get("workStates")
        if isinstance(work_states, list) and work_states:
            for code in work_states:
                normalized = normalize_state(str(code))
                if normalized:
                    state_code = normalized
                    break

        for key in ("placeOfPerformance", "placeOfPerformanceLocation"):
            block = sam_raw.get(key)
            if isinstance(block, dict):
                city, state_code, zip_code = _parse_sam_location_block(block)
                if state_code:
                    break

    if not state_code and location:
        parts = [part.strip() for part in location.split(",") if part.strip()]
        state_idx: int | None = None
        for idx in range(len(parts) - 1, -1, -1):
            part = parts[idx]
            if re.fullmatch(r"\d{5}(?:-\d{4})?", part):
                zip_code = part[:5]
                continue
            code = normalize_state(part)
            if code:
                state_code = code
                state_idx = idx
                break
        if state_code and state_idx is not None and state_idx > 0:
            city = ", ".join(parts[:state_idx]) or None
        elif state_code and len(parts) == 1:
            city = None

    if not state_code and location:
        match = re.search(r"\b([A-Z]{2})\b", location.upper())
        if match:
            state_code = normalize_state(match.group(1))

    label = format_location_scope(state_code, city)
    work_states = sam_raw.get("workStates") if isinstance(sam_raw, dict) else None
    if isinstance(work_states, list) and len(work_states) > 1:
        label = f"Multiple states ({', '.join(work_states)})"

    return {
        "state_code": state_code,
        "city": city,
        "zip": zip_code,
        "label": label,
        "work_states": work_states if isinstance(work_states, list) else [],
    }


def format_location_scope(state_code: str | None, city: str | None = None) -> str | None:
    if not state_code:
        return None
    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)
    if city:
        return f"{city}, {state_name}"
    return state_name


def extract_state(location: str | None, sam_raw: dict[str, Any] | None = None) -> str | None:
    """Pull a two-letter state code from place of performance."""
    return extract_work_location(location, sam_raw).get("state_code")


def _parse_award_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = str(value).strip()[:10]
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _recency_weight(award_date: date, today: date | None = None) -> float:
    """Exponential decay — awards from ~1 year ago weigh 2x more than ~3 years ago."""
    today = today or date.today()
    days_ago = max(0, (today - award_date).days)
    return 0.5 ** (days_ago / 365.0)


def _weighted_percentile(pairs: list[tuple[float, float]], pct: float) -> float:
    """Percentile using recency weights. pairs = [(amount, weight), ...]"""
    if not pairs:
        raise ValueError("pairs required")
    if len(pairs) == 1:
        return pairs[0][0]
    ordered = sorted(pairs, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    target = total_weight * (pct / 100.0)
    cumulative = 0.0
    for amount, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return amount
    return ordered[-1][0]


def _normalize_award(row: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    amount = row.get("Award Amount")
    try:
        amount_value = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_value = None

    start_raw = row.get("Start Date")
    award_date = _parse_award_date(start_raw)
    days_ago = (today - award_date).days if award_date else None
    recency_weight = round(_recency_weight(award_date, today), 3) if award_date else None

    pop_state = normalize_state(str(row.get("Place of Performance State Code") or ""))
    pop_city = str(row.get("Place of Performance City Name") or "").strip() or None
    pop_zip = str(row.get("Place of Performance Zip5") or "").strip()[:5] or None
    location_parts = [p for p in (pop_city, pop_state, pop_zip) if p]
    performance_location = ", ".join(location_parts) if location_parts else None

    return {
        "award_id": row.get("Award ID"),
        "recipient_name": row.get("Recipient Name"),
        "award_amount": amount_value,
        "award_date": award_date.isoformat() if award_date else None,
        "start_date": start_raw,
        "end_date": row.get("End Date"),
        "days_ago": days_ago,
        "recency_weight": recency_weight,
        "awarding_agency": row.get("Awarding Agency"),
        "contract_award_type": row.get("Contract Award Type"),
        "description": str(row.get("Description") or "").strip() or None,
        "performance_state": pop_state,
        "performance_city": pop_city,
        "performance_zip": pop_zip,
        "performance_location": performance_location,
    }


def build_search_payload(
    naics_code: str,
    state_codes: str | list[str],
    *,
    city: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * 3)
    if isinstance(state_codes, str):
        state_codes = [state_codes]
    if city and len(state_codes) == 1:
        locations: list[dict[str, str]] = [{"country": "USA", "state": state_codes[0], "city": city}]
    else:
        locations = [{"country": "USA", "state": code} for code in state_codes]
    return {
        "filters": {
            "naics_codes": {"require": [naics_code]},
            "place_of_performance_scope": "domestic",
            "place_of_performance_locations": locations,
            "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
            "time_period": [
                {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                }
            ],
        },
        "fields": AWARD_FIELDS,
        "sort": "Start Date",
        "order": "desc",
        "page": 1,
        "limit": limit,
    }


def _count_dated_awards(awards: list[dict[str, Any]]) -> int:
    return sum(
        1
        for award in awards
        if award.get("award_date") and award.get("award_amount") and award["award_amount"] > 0
    )


def _filter_awards_by_states(
    awards: list[dict[str, Any]],
    allowed_states: set[str],
) -> list[dict[str, Any]]:
    """Drop any award whose place of performance is outside the allowed states."""
    matched: list[dict[str, Any]] = []
    for award in awards:
        pop_state = award.get("performance_state")
        if pop_state and pop_state not in allowed_states:
            continue
        matched.append(award)
    return matched


def _filter_awards_by_state(awards: list[dict[str, Any]], state_code: str) -> list[dict[str, Any]]:
    return _filter_awards_by_states(awards, {state_code})


def _query_awards(
    naics_code: str,
    state_code: str,
    *,
    city: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _query_awards_in_states(naics_code, [state_code], city=city, limit=limit)


def _query_awards_in_states(
    naics_code: str,
    state_codes: list[str],
    *,
    city: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    payload = build_search_payload(naics_code, state_codes, city=city, limit=limit)
    url = f"{BASE_URL}{SEARCH_PATH}"
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    today = date.today()
    allowed = set(state_codes)
    awards = [_normalize_award(row, today) for row in (data.get("results") or [])[:limit]]
    awards = _filter_awards_by_states(awards, allowed)
    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards


def summarize_awards(
    awards: list[dict[str, Any]],
    *,
    naics_code: str,
    state_code: str,
    location_scope: str | None = None,
    location_scope_type: str | None = None,
    location_scope_note: str | None = None,
    surrounding_states: list[str] | None = None,
    scope_profile: dict[str, Any] | None = None,
    unit_rate_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = date.today()
    dated_awards = [
        a
        for a in awards
        if a.get("award_date") and a.get("award_amount") and a["award_amount"] > 0
    ]
    dated_awards.sort(key=lambda a: a["award_date"], reverse=True)

    amounts = [a["award_amount"] for a in dated_awards]
    weighted_pairs = [
        (a["award_amount"], a["recency_weight"])
        for a in dated_awards
        if a.get("recency_weight")
    ]

    recipient_weights: Counter[str] = Counter()
    for award in dated_awards:
        name = str(award.get("recipient_name") or "").strip()
        if name and award.get("recency_weight"):
            recipient_weights[name] += award["recency_weight"]

    top_winner, top_winner_score = recipient_weights.most_common(1)[0] if recipient_weights else (None, 0)
    incumbent = dated_awards[0]["recipient_name"] if dated_awards else None

    awards_last_12_months = sum(
        1 for a in dated_awards if a.get("days_ago") is not None and a["days_ago"] <= 365
    )

    summary: dict[str, Any] = {
        "naics_code": naics_code,
        "state_code": state_code,
        "location_scope": location_scope or format_location_scope(state_code),
        "location_scope_type": location_scope_type or "state",
        "location_scope_note": location_scope_note,
        "surrounding_states": surrounding_states or [],
        "lookback_years": 3,
        "awards_count": len(awards),
        "awards_with_dates": len(dated_awards),
        "awards_missing_dates": len(awards) - len([a for a in awards if a.get("award_date")]),
        "awards_last_12_months": awards_last_12_months,
        "awards_with_amounts": len(amounts),
        "unique_bidders": len(recipient_weights),
        "average_amount": None,
        "weighted_average_amount": None,
        "highest_amount": None,
        "lowest_amount": None,
        "most_frequent_winner": top_winner,
        "most_frequent_winner_count": round(top_winner_score, 1) if top_winner_score else 0,
        "likely_incumbent": incumbent,
        "recommended_bid_low": None,
        "recommended_bid_high": None,
        "recommended_bid_note": None,
        "newest_award_date": dated_awards[0]["award_date"] if dated_awards else None,
        "oldest_award_date": dated_awards[-1]["award_date"] if dated_awards else None,
        "awards": awards,
    }

    if not amounts:
        summary["recommended_bid_note"] = (
            "No dated award amounts returned for comparable contracts."
        )
        return summary

    total_weight = sum(weight for _, weight in weighted_pairs)
    weighted_avg = sum(amount * weight for amount, weight in weighted_pairs) / total_weight
    summary["average_amount"] = round(statistics.mean(amounts), 2)
    summary["weighted_average_amount"] = round(weighted_avg, 2)
    summary["highest_amount"] = round(max(amounts), 2)
    summary["lowest_amount"] = round(min(amounts), 2)

    if unit_rate_summary:
        summary["unit_rate_summary"] = unit_rate_summary
        if unit_rate_summary.get("recommended_annual_bid") is not None:
            summary["recommended_annual_bid"] = unit_rate_summary["recommended_annual_bid"]
            summary["recommended_bid_formula"] = unit_rate_summary.get("recommended_bid_formula")
            summary["recommended_bid_low"] = unit_rate_summary.get("recommended_bid_low")
            summary["recommended_bid_high"] = unit_rate_summary.get("recommended_bid_high")
            summary["recommended_bid_note"] = unit_rate_summary.get("recommended_bid_note")
        elif unit_rate_summary.get("recommended_bid_note"):
            summary["recommended_bid_note"] = unit_rate_summary["recommended_bid_note"]
    else:
        summary["recommended_bid_note"] = (
            "Comparable award totals shown in the table — recommended annual bid requires "
            "unit rates ($/sq ft per visit) from your contract scope."
        )

    return summary


def fetch_pricing_intelligence(
    naics_code: str | None,
    state_code: str | None,
    *,
    city: str | None = None,
    zip_code: str | None = None,
    origin_location: dict[str, Any] | None = None,
    scope_profile: dict[str, Any] | None = None,
    limit: int = COMPARABLE_DISPLAY_LIMIT,
) -> dict[str, Any]:
    """Query USAspending.gov for comparable contracts; normalize to $/sq ft per visit."""
    from comparable_scope import (
        filter_clearance_compatible_awards,
        pricing_allow_neighbor_states,
        pricing_max_distance_miles,
        summarize_unit_rates,
    )
    from geo import filter_local_awards

    if not naics_code:
        raise ValueError("NAICS code is required for pricing lookup.")
    if not state_code:
        raise ValueError("Could not determine the contract state for pricing lookup.")

    fetch_limit = SEARCH_FETCH_LIMIT
    max_miles = pricing_max_distance_miles()
    allow_neighbors = pricing_allow_neighbor_states()

    origin = origin_location or {
        "state_code": state_code,
        "city": city,
        "zip": zip_code,
        "label": format_location_scope(state_code, city),
    }
    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)
    neighbors = BORDERING_STATES.get(state_code, [])
    awards: list[dict[str, Any]]
    location_scope: str
    location_scope_type = "state"
    location_scope_note: str | None = None
    surrounding_states: list[str] = []

    if city:
        local_awards = _query_awards(naics_code, state_code, city=city, limit=fetch_limit)
        local_dated = _count_dated_awards(local_awards)
        if local_dated >= MIN_LOCAL_COMPARABLE_AWARDS:
            awards = local_awards
            location_scope = format_location_scope(state_code, city) or f"{city}, {state_name}"
            location_scope_type = "city"
        else:
            state_awards = _query_awards(naics_code, state_code, limit=fetch_limit)
            state_dated = _count_dated_awards(state_awards)
            if state_dated >= MIN_LOCAL_COMPARABLE_AWARDS or not neighbors:
                awards = state_awards
                location_scope = state_name
                location_scope_note = (
                    f"Only {local_dated} recent comparable award(s) in {city}; "
                    f"expanded to all contracts performed in {state_name}."
                    if local_dated > 0
                    else f"No recent comparable awards in {city}; showing contracts performed statewide in {state_name}."
                )
            else:
                if allow_neighbors:
                    awards, location_scope, location_scope_note, surrounding_states = _regional_fallback(
                        naics_code,
                        state_code,
                        state_name,
                        neighbors,
                        limit=fetch_limit,
                        prior_note=(
                            f"Few comparables in {city} and {state_name}; "
                            f"expanded to neighboring states within {max_miles} mi."
                        ),
                    )
                    location_scope_type = "region"
                else:
                    awards = state_awards
                    location_scope = state_name
                    location_scope_note = (
                        f"Only {local_dated} local award(s) in {city}; "
                        f"showing same-state contracts within {max_miles} mi (neighboring states disabled)."
                        if local_dated > 0
                        else f"No local awards in {city}; showing same-state contracts within {max_miles} mi."
                    )
    else:
        state_awards = _query_awards(naics_code, state_code, limit=fetch_limit)
        state_dated = _count_dated_awards(state_awards)
        if state_dated >= MIN_LOCAL_COMPARABLE_AWARDS or not neighbors:
            awards = state_awards
            location_scope = state_name
        elif allow_neighbors:
            awards, location_scope, location_scope_note, surrounding_states = _regional_fallback(
                naics_code,
                state_code,
                state_name,
                neighbors,
                limit=fetch_limit,
                prior_note=(
                    f"Only {state_dated} recent comparable award(s) in {state_name}; "
                    f"expanded to neighboring states within {max_miles} mi."
                ),
            )
            location_scope_type = "region"
        else:
            awards = state_awards
            location_scope = state_name
            location_scope_note = (
                f"Only {state_dated} award(s) in {state_name}; "
                f"staying in-state within {max_miles} mi (neighboring states disabled)."
            )

    awards = annotate_award_distances(awards, origin, state_names=STATE_CODE_TO_NAME)

    awards, geo_meta = filter_local_awards(
        awards,
        origin_state=state_code,
        max_miles=max_miles,
        require_same_state=not allow_neighbors,
    )
    if geo_meta.get("dropped_out_of_range") or geo_meta.get("dropped_other_state"):
        geo_note = (
            f"Geography filter: kept {geo_meta['local_count']} award(s) within {max_miles} mi"
            f"{' and in ' + state_name if not allow_neighbors else ''}."
        )
        location_scope_note = f"{location_scope_note} {geo_note}".strip() if location_scope_note else geo_note

    profile = scope_profile or {}
    awards, scope_meta = filter_clearance_compatible_awards(awards, profile)
    awards = awards[:limit]
    if scope_meta.get("scope_note"):
        location_scope_note = (
            f"{location_scope_note} {scope_meta['scope_note']}"
            if location_scope_note
            else scope_meta["scope_note"]
        )

    unit_rate_summary = summarize_unit_rates(awards, profile)

    summary = summarize_awards(
        awards,
        naics_code=naics_code,
        state_code=state_code,
        location_scope=location_scope,
        location_scope_type=location_scope_type,
        location_scope_note=location_scope_note,
        surrounding_states=surrounding_states,
        scope_profile=profile,
        unit_rate_summary=unit_rate_summary,
    )
    summary["origin_location"] = origin
    if awards:
        closest = awards[0]
        summary["closest_award_miles"] = closest.get("distance_miles")
        summary["closest_award_label"] = closest.get("distance_label")
        summary["closest_award_location"] = closest.get("performance_location")
    summary["source"] = "USAspending.gov"
    summary["fetched_at"] = date.today().isoformat()
    summary["scope_profile"] = profile
    summary["scope_matching"] = scope_meta
    summary["unit_rate_summary"] = unit_rate_summary
    summary["geo_filter"] = geo_meta
    return summary


def _regional_fallback(
    naics_code: str,
    state_code: str,
    state_name: str,
    neighbors: list[str],
    *,
    limit: int,
    prior_note: str,
) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    """Widest allowed search: home state plus bordering states only."""
    regional_states = [state_code, *neighbors]
    awards = _query_awards_in_states(naics_code, regional_states, limit=limit)
    location_scope = f"{state_name} and neighboring states"
    location_scope_note = (
        f"{prior_note} Includes {_state_names(regional_states)}. "
        "National or distant-state awards are never included."
    )
    return awards, location_scope, location_scope_note, neighbors
