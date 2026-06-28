"""Constants for two-tier pricing intelligence."""

from __future__ import annotations

MIN_REGIONAL_AWARD_AMOUNT = 10_000
MIN_INTERNAL_PRICING_MATCHES = 3
INTERNAL_BID_LOW_FACTOR = 0.90
INTERNAL_BID_HIGH_FACTOR = 1.10

BUILDING_TYPES: tuple[str, ...] = (
    "office",
    "medical",
    "warehouse",
    "military",
    "courthouse",
    "other",
)

US_MACRO_REGIONS: dict[str, list[str]] = {
    "Northeast": ["ME", "NH", "VT", "MA", "RI", "CT", "NY", "NJ", "PA"],
    "Southeast": [
        "DE", "MD", "DC", "VA", "WV", "NC", "SC", "GA", "FL", "KY", "TN", "AL", "MS", "AR", "LA",
    ],
    "Midwest": ["OH", "MI", "IN", "IL", "WI", "MN", "IA", "MO", "ND", "SD", "NE", "KS"],
    "Southwest": ["TX", "OK", "NM", "AZ"],
    "West": ["CO", "WY", "MT", "ID", "WA", "OR", "CA", "NV", "UT", "AK", "HI"],
}

STATE_TO_MACRO_REGION: dict[str, str] = {
    state: region for region, states in US_MACRO_REGIONS.items() for state in states
}


def regional_confidence(count: int) -> tuple[str, str]:
    if count >= 10:
        return "high", "High Confidence"
    if count >= 5:
        return "medium", "Medium Confidence"
    return "low", "Low Confidence — Limited Data"
