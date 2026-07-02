"""Adjust contract scores based on subcontractor proximity."""

from __future__ import annotations

from typing import Any

from models import Contract, ContractSub, Sub
from settings_store import get_sub_search_settings
from sub_finder import _contract_coords, haversine_miles


def _nearest_sub_distance(session, contract: Contract) -> tuple[float | None, int, int]:
    """Return (nearest_miles, subs_within_radius, linked_sub_count)."""
    settings = get_sub_search_settings()
    radius = float(settings["search_radius_miles"])

    linked = (
        session.query(ContractSub)
        .filter_by(contract_id=contract.id)
        .filter(ContractSub.distance_miles.isnot(None))
        .all()
    )
    if linked:
        distances = [float(link.distance_miles) for link in linked if link.distance_miles is not None]
        if distances:
            nearest = min(distances)
            within = sum(1 for d in distances if d <= radius)
            return nearest, within, len(distances)

    try:
        lat, lng, _work = _contract_coords(contract)
    except ValueError:
        return None, 0, 0
    if lat is None or lng is None:
        return None, 0, 0

    distances: list[float] = []
    for sub in session.query(Sub).filter(Sub.latitude.isnot(None), Sub.longitude.isnot(None)).all():
        dist = haversine_miles(lat, lng, float(sub.latitude), float(sub.longitude))
        if dist <= radius:
            distances.append(dist)

    if not distances:
        return None, 0, 0
    return min(distances), len(distances), 0


def adjust_score_for_proximity(
    base_score: int | float | None,
    *,
    nearest_miles: float | None,
    subs_within_radius: int,
    search_radius_miles: float,
) -> dict[str, Any]:
    """Apply a distance penalty so remote contracts (e.g. Ely, NV) don't score 8/10."""
    if base_score is None:
        return {
            "base_score": None,
            "effective_score": None,
            "proximity_penalty": 0,
            "proximity_note": None,
            "nearest_sub_miles": nearest_miles,
            "subs_within_radius": subs_within_radius,
        }

    score = int(round(float(base_score)))
    penalty = 0
    note: str | None = None

    if subs_within_radius == 0:
        if nearest_miles is None:
            penalty = 4
            note = "No subs found nearby — staffing will be very hard"
            score = min(score, 4)
        elif nearest_miles > 100:
            penalty = 5
            note = f"Nearest sub {nearest_miles:.0f} mi away — nearly impossible to staff"
            score = min(score, 3)
        elif nearest_miles > 75:
            penalty = 4
            note = f"Nearest sub {nearest_miles:.0f} mi away — very hard to staff"
            score = min(score, 4)
        elif nearest_miles > 50:
            penalty = 3
            note = f"Nearest sub {nearest_miles:.0f} mi away — hard to staff"
            score = min(score, 5)
        elif nearest_miles > search_radius_miles:
            penalty = 2
            note = f"Nearest sub {nearest_miles:.0f} mi away — outside search radius"
            score = max(1, score - 2)
    elif nearest_miles is not None:
        if nearest_miles > 40:
            penalty = 2
            note = f"Nearest sub {nearest_miles:.0f} mi away"
            score = max(1, score - 2)
        elif nearest_miles > 25:
            penalty = 1
            note = f"Nearest sub {nearest_miles:.0f} mi away"
            score = max(1, score - 1)

    return {
        "base_score": int(round(float(base_score))),
        "effective_score": max(1, min(10, score)),
        "proximity_penalty": penalty,
        "proximity_note": note,
        "nearest_sub_miles": round(nearest_miles, 1) if nearest_miles is not None else None,
        "subs_within_radius": subs_within_radius,
    }


def proximity_context(session, contract: Contract) -> dict[str, Any]:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    base_score = analysis.get("score")
    if base_score is None:
        base_score = analysis.get("text_score")

    settings = get_sub_search_settings()
    nearest, within, _linked = _nearest_sub_distance(session, contract)
    searched = getattr(contract, "sub_search_status", None) in ("complete", "searching")
    result = adjust_score_for_proximity(
        base_score,
        nearest_miles=nearest,
        subs_within_radius=within,
        search_radius_miles=float(settings["search_radius_miles"]),
    )
    if not searched and within == 0 and nearest is None:
        result["proximity_penalty"] = 0
        result["effective_score"] = int(round(float(base_score))) if base_score is not None else None
        result["proximity_note"] = "Run Find Subs to check local market"
    return result
