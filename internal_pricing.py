"""Tier 2 — internal pricing database queries and dashboard aggregates."""

from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models import Contract
from pricing_constants import (
    INTERNAL_BID_HIGH_FACTOR,
    INTERNAL_BID_LOW_FACTOR,
    MIN_INTERNAL_PRICING_MATCHES,
    STATE_TO_MACRO_REGION,
    regional_confidence,
)
from pws_fields import annual_visits, pws_snapshot


def _sqft_in_range(candidate_sqft: int | None, target: int | None) -> bool:
    if not candidate_sqft or not target:
        return False
    low = target * 0.5
    high = target * 1.5
    return low <= candidate_sqft <= high


def _macro_region(state: str | None) -> str | None:
    if not state:
        return None
    return STATE_TO_MACRO_REGION.get(state.upper())


def _match_tier(contract: Contract, candidate: Contract) -> int | None:
    """Return 1 (best), 2, 3, or None if not a match."""
    if candidate.id == contract.id:
        return None
    if candidate.price_per_sqft_per_visit is None:
        return None
    if not candidate.square_footage or not contract.square_footage:
        return None
    if contract.naics_code and candidate.naics_code != contract.naics_code:
        return None
    if not _sqft_in_range(candidate.square_footage, contract.square_footage):
        return None

    contract_state = (contract.pricing_region or "").upper()
    candidate_state = (candidate.pricing_region or "").upper()

    same_state = contract_state and contract_state == candidate_state
    same_type = (
        contract.building_type
        and candidate.building_type
        and contract.building_type == candidate.building_type
    )
    same_region = (
        contract_state
        and candidate_state
        and _macro_region(contract_state) == _macro_region(candidate_state)
    )

    if same_state and same_type:
        return 1
    if same_state:
        return 2
    if same_region:
        return 3
    return None


def query_internal_pricing(session: Session, contract: Contract) -> dict[str, Any]:
    """Find similar contracts in our database with known $/sq ft/visit."""
    rows = (
        session.query(Contract)
        .filter(Contract.price_per_sqft_per_visit.isnot(None))
        .filter(Contract.square_footage.isnot(None))
        .all()
    )

    tiers: dict[int, list[Contract]] = {1: [], 2: [], 3: []}
    for row in rows:
        tier = _match_tier(contract, row)
        if tier:
            tiers[tier].append(row)

    matched: list[Contract] = []
    match_tier_used = None
    for tier in (1, 2, 3):
        if len(tiers[tier]) >= MIN_INTERNAL_PRICING_MATCHES:
            matched = tiers[tier]
            match_tier_used = tier
            break

    if len(matched) < MIN_INTERNAL_PRICING_MATCHES:
        return {
            "available": False,
            "message": (
                "Not enough internal data yet for this contract type and region. "
                "Internal pricing accuracy improves as you analyze more contracts."
            ),
            "match_count": max(len(tiers[1]), len(tiers[2]), len(tiers[3])),
        }

    from location_matching import prioritize_matched_contracts, extract_site_profile

    matched = prioritize_matched_contracts(contract, matched)
    origin = extract_site_profile(contract)
    today = date.today()
    rates = [float(r.price_per_sqft_per_visit) for r in matched]
    avg_rate = statistics.mean(rates)
    sqft = contract.square_footage or 0
    visits = annual_visits(contract.cleaning_frequency_per_week)
    visits_f = float(visits) if visits else None

    recommended = None
    bid_low = None
    bid_high = None
    if sqft and visits_f:
        recommended = round(avg_rate * sqft * visits_f, 2)
        bid_low = round(avg_rate * INTERNAL_BID_LOW_FACTOR * sqft * visits_f, 2)
        bid_high = round(avg_rate * INTERNAL_BID_HIGH_FACTOR * sqft * visits_f, 2)

    conf_key, conf_label = regional_confidence(len(matched))
    tier_labels = {
        1: "same NAICS, state, building type, and similar square footage",
        2: "same NAICS, state, and similar square footage",
        3: "same NAICS, region, and similar square footage",
    }

    return {
        "available": True,
        "match_count": len(matched),
        "match_tier": match_tier_used,
        "match_description": tier_labels.get(match_tier_used or 0, ""),
        "avg_price_per_sqft_per_visit": round(avg_rate, 6),
        "recommended_annual_bid": recommended,
        "recommended_bid_low": bid_low,
        "recommended_bid_high": bid_high,
        "confidence": conf_key,
        "confidence_label": conf_label,
        "formula_note": (
            f"Average ${avg_rate:.4f}/sq ft/visit × {sqft:,} sq ft × {int(visits_f)} visits/yr"
            if recommended and visits_f
            else None
        ),
        "matched_contracts": [
            _matched_contract_row(contract, r, origin=origin, today=today)
            for r in matched[:10]
        ],
    }


def _matched_contract_row(
    contract: Contract,
    row: Contract,
    *,
    origin: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    from location_matching import extract_site_profile, same_site_and_scope

    row_profile = extract_site_profile(row)
    same = same_site_and_scope(origin, row_profile)
    expired = bool(row.due_date and row.due_date < today)
    note = None
    if same and expired:
        note = "Same address & scope — prior solicitation expired"
    elif same:
        note = "Same address & scope"
    return {
        "notice_id": row.notice_id,
        "title": row.title,
        "location": row.location,
        "street_address": row_profile.get("street_address"),
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "square_footage": row.square_footage,
        "building_type": row.building_type,
        "pricing_region": row.pricing_region,
        "naics_code": row.naics_code,
        "price_per_sqft_per_visit": float(row.price_per_sqft_per_visit),
        "awarded_amount": float(row.awarded_amount) if row.awarded_amount else None,
        "status": row.status,
        "same_location": same,
        "expired": expired,
        "location_note": note,
    }


def build_pricing_dashboard(session: Session) -> dict[str, Any]:
    """Aggregate stats for the Pricing Intelligence dashboard."""
    rows = session.query(Contract).all()
    with_sqft = [r for r in rows if r.square_footage]
    with_rates = [r for r in rows if r.price_per_sqft_per_visit is not None]

    by_naics: dict[str, int] = {}
    by_state: dict[str, int] = {}
    rate_by_state: dict[str, list[float]] = {}

    for row in with_sqft:
        code = row.naics_code or "unknown"
        by_naics[code] = by_naics.get(code, 0) + 1
        state = row.pricing_region or "unknown"
        by_state[state] = by_state.get(state, 0) + 1

    for row in with_rates:
        state = row.pricing_region or "unknown"
        rate_by_state.setdefault(state, []).append(float(row.price_per_sqft_per_visit))

    heatmap = {
        state: round(statistics.mean(vals), 6)
        for state, vals in sorted(rate_by_state.items())
        if vals
    }

    won = [r for r in rows if r.status == "won" and r.awarded_amount]
    lost = [r for r in rows if r.status == "lost" and r.awarded_amount]
    decided = len(won) + len(lost)

    win_buckets = {"low": {"won": 0, "lost": 0}, "mid": {"won": 0, "lost": 0}, "high": {"won": 0, "lost": 0}}
    for row in won + lost:
        intel = row.pricing_intel if isinstance(row.pricing_intel, dict) else {}
        intel = row.pricing_intel if isinstance(row.pricing_intel, dict) else {}
        snapshot = intel.get("internal_snapshot") or intel.get("internal") or {}
        low = snapshot.get("recommended_bid_low")
        high = snapshot.get("recommended_bid_high")
        amount = float(row.awarded_amount)
        if low and high:
            if amount < low:
                bucket = "low"
            elif amount > high:
                bucket = "high"
            else:
                bucket = "mid"
        else:
            bucket = "mid"
        key = "won" if row.status == "won" else "lost"
        win_buckets[bucket][key] += 1

    margin_by_region: dict[str, list[float]] = {}
    for row in won:
        if not row.selected_sub_quote or not row.awarded_amount:
            continue
        sub = float(row.selected_sub_quote)
        bid = float(row.awarded_amount)
        if bid <= 0 or sub >= bid:
            continue
        margin = (bid - sub) / bid
        region = row.pricing_region or "unknown"
        margin_by_region.setdefault(region, []).append(margin)

    recommended_margin = {
        state: round(statistics.mean(vals) * 100, 1)
        for state, vals in margin_by_region.items()
        if vals
    }

    return {
        "total_in_database": len(with_sqft),
        "total_with_unit_rates": len(with_rates),
        "by_naics": by_naics,
        "by_state": by_state,
        "avg_price_per_sqft_per_visit_by_state": heatmap,
        "win_rate_overall": round(len(won) / decided, 2) if decided else None,
        "win_rate_by_price_range": win_buckets,
        "recommended_margin_by_region_pct": recommended_margin,
        "pws_snapshot_count": len(with_sqft),
    }
