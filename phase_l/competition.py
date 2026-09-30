"""Phase L competition intelligence — open vs pre-filtered offer counts."""

from __future__ import annotations

from typing import Any

from phase_l.access_gate import (
    BPA_ONLY,
    GWAC_ONLY,
    IDIQ_ONLY,
    MAS_ONLY,
    OPEN_MARKET,
    SEWP_ONLY,
    SOLE_SOURCE,
    SOURCE_APPROVAL_REQUIRED,
    SPECIAL_SET_ASIDE,
    TOTAL_SMALL_BUSINESS,
    UNRESTRICTED,
    VEHICLE_ONLY,
)

VERY_LOW_OPEN_COMPETITION = "VERY_LOW_OPEN_COMPETITION"
LOW_OPEN_COMPETITION = "LOW_OPEN_COMPETITION"
MODERATE_OPEN_COMPETITION = "MODERATE_OPEN_COMPETITION"
HIGH_OPEN_COMPETITION = "HIGH_OPEN_COMPETITION"
PRE_FILTERED_COMPETITION = "PRE_FILTERED_COMPETITION"
UNKNOWN = "UNKNOWN"

_PREFILTERED_TYPES = {
    VEHICLE_ONLY,
    BPA_ONLY,
    IDIQ_ONLY,
    GWAC_ONLY,
    MAS_ONLY,
    SEWP_ONLY,
    SOLE_SOURCE,
    SOURCE_APPROVAL_REQUIRED,
}

_OPENISH = {OPEN_MARKET, UNRESTRICTED, TOTAL_SMALL_BUSINESS}


def effective_competition_signal(
    *,
    historical_offers_received: int | float | None,
    historical_competition_type: str | None = None,
    competition_access_type: str | None = None,
) -> str:
    """Offer counts are only comparable when history was genuinely open/accessible."""
    ctype = (historical_competition_type or competition_access_type or "").upper()
    if ctype in _PREFILTERED_TYPES or ctype == PRE_FILTERED_COMPETITION:
        return PRE_FILTERED_COMPETITION
    if historical_offers_received is None:
        return UNKNOWN
    try:
        n = int(historical_offers_received)
    except (TypeError, ValueError):
        return UNKNOWN
    if ctype and ctype not in _OPENISH and ctype != SPECIAL_SET_ASIDE:
        # Unknown type with offers — do not claim open
        if ctype not in {"", UNKNOWN, "UNKNOWN"}:
            return PRE_FILTERED_COMPETITION
    if n <= 1:
        return VERY_LOW_OPEN_COMPETITION
    if n <= 3:
        return LOW_OPEN_COMPETITION
    if n <= 5:
        return MODERATE_OPEN_COMPETITION
    if n <= 20:
        return HIGH_OPEN_COMPETITION
    return HIGH_OPEN_COMPETITION


def competition_attractiveness_rank(signal: str, offers: int | None) -> int:
    """Higher = more attractive. PRE_FILTERED never ranks as low-competition win."""
    if signal == PRE_FILTERED_COMPETITION:
        return 0
    if signal == UNKNOWN or offers is None:
        return 1
    if offers <= 1:
        return 100
    if offers <= 3:
        return 90
    if offers <= 5:
        return 75
    if offers <= 10:
        return 55
    if offers <= 20:
        return 35
    return 15


def annotate_competition(
    *,
    historical_offers_received: int | float | None = None,
    historical_competition_type: str | None = None,
    competition_access_type: str | None = None,
    historical_accessibility_match: str | None = None,
) -> dict[str, Any]:
    signal = effective_competition_signal(
        historical_offers_received=historical_offers_received,
        historical_competition_type=historical_competition_type,
        competition_access_type=competition_access_type,
    )
    offers = None
    if historical_offers_received is not None:
        try:
            offers = int(historical_offers_received)
        except (TypeError, ValueError):
            offers = None
    return {
        "historical_offers_received": offers,
        "historical_competition_type": historical_competition_type or competition_access_type,
        "historical_accessibility_match": historical_accessibility_match,
        "effective_competition_signal": signal,
        "competition_rank_score": competition_attractiveness_rank(signal, offers),
        "is_open_comparable": signal != PRE_FILTERED_COMPETITION and signal != UNKNOWN,
    }


def offer_bucket(offers: int | None) -> str:
    if offers is None:
        return "UNKNOWN"
    if offers <= 1:
        return "1"
    if offers == 2:
        return "2"
    if offers == 3:
        return "3"
    if offers <= 5:
        return "4-5"
    if offers <= 10:
        return "6-10"
    if offers <= 20:
        return "11-20"
    return "20+"
