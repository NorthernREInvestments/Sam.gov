"""USAspending.gov historical award search for pricing intelligence."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import date, timedelta
from typing import Any

import httpx

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
]

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


def extract_state(location: str | None, sam_raw: dict[str, Any] | None = None) -> str | None:
    """Pull a two-letter state code from contract location or SAM.gov raw record."""
    if sam_raw:
        for key in ("placeOfPerformance", "officeAddress", "placeOfPerformanceLocation"):
            block = sam_raw.get(key)
            if isinstance(block, dict):
                for field in ("state", "stateCode", "state_code", "code"):
                    code = normalize_state(str(block.get(field) or ""))
                    if code:
                        return code

    if not location:
        return None

    parts = [part.strip() for part in location.split(",") if part.strip()]
    for part in reversed(parts):
        code = normalize_state(part)
        if code:
            return code

    match = re.search(r"\b([A-Z]{2})\b", location.upper())
    if match:
        return normalize_state(match.group(1))
    return None


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
    }


def build_search_payload(naics_code: str, state_code: str, limit: int = 20) -> dict[str, Any]:
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * 3)
    return {
        "filters": {
            "naics_codes": {"require": [naics_code]},
            "place_of_performance_locations": [{"country": "USA", "state": state_code}],
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


def summarize_awards(
    awards: list[dict[str, Any]],
    *,
    naics_code: str,
    state_code: str,
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

    if len(weighted_pairs) >= 4:
        low = _weighted_percentile(weighted_pairs, 25)
        high = _weighted_percentile(weighted_pairs, 75)
        summary["recommended_bid_note"] = (
            "Suggested range based on recency-weighted awards — last 12 months count roughly "
            "twice as much as awards from 2–3 years ago."
        )
    else:
        low = min(amounts)
        high = max(amounts)
        summary["recommended_bid_note"] = (
            "Limited dated comparable awards — suggested range spans the observed low and high."
        )

    summary["recommended_bid_low"] = round(low, 2)
    summary["recommended_bid_high"] = round(high, 2)
    return summary


def fetch_pricing_intelligence(
    naics_code: str | None,
    state_code: str | None,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Query USAspending.gov and return pricing intelligence for similar contracts."""
    if not naics_code:
        raise ValueError("NAICS code is required for pricing lookup.")
    if not state_code:
        raise ValueError("Could not determine the contract state for pricing lookup.")

    payload = build_search_payload(naics_code, state_code, limit=limit)
    url = f"{BASE_URL}{SEARCH_PATH}"

    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    raw_results = data.get("results") or []
    today = date.today()
    awards = [_normalize_award(row, today) for row in raw_results[:limit]]
    awards.sort(
        key=lambda a: a.get("award_date") or "",
        reverse=True,
    )
    summary = summarize_awards(awards, naics_code=naics_code, state_code=state_code)
    summary["source"] = "USAspending.gov"
    summary["fetched_at"] = date.today().isoformat()
    return summary
