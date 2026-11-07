"""Configurable owner profit targets — hard 15% margin + dollar floor.

Authoritative thresholds live in verified_commercial_decision_v1.
This module re-exports owner-facing status labels for compatibility.
"""

from __future__ import annotations

from typing import Any

from verified_commercial_decision_v1 import (
    BUILD as VCD_BUILD,
    DEFAULT_MIN_MARGIN,
    DEFAULT_MIN_PROFIT_USD,
    EXECUTION_BLOCKED,
    LIKELY_PROFITABLE,
    POSSIBLE_PROFIT,
    PROVEN,
    PROVEN_PROFITABLE,
    REJECT,
    UNPROFITABLE,
    UNPROVEN,
    VERIFY,
    load_commercial_targets,
)

# Price-basis proof signals
PROFITABLE_AT_PUBLIC_RETAIL = "PROFITABLE_AT_PUBLIC_RETAIL"
PROFITABLE_AT_QUOTED_COST = "PROFITABLE_AT_QUOTED_COST"
PROFITABLE_AT_DISTRIBUTOR_COST = "PROFITABLE_AT_DISTRIBUTOR_COST"
PROFITABLE_WITH_PERMITTED_EQUAL = "PROFITABLE_WITH_PERMITTED_EQUAL"

BUILD = f"{VCD_BUILD}-profit-first-config"


def owner_targets(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Owner-configurable thresholds. Margin ≥15% is always enforced."""
    t = load_commercial_targets(overrides)
    return {
        "minimum_expected_profit_usd": float(t["minimum_net_profit_usd"]),
        "minimum_post_financing_profit_usd": float(t["minimum_net_profit_usd"]),
        "minimum_margin_pct": float(t["minimum_net_margin"]) * 100.0,  # percent form for legacy callers
        "minimum_net_margin": float(t["minimum_net_margin"]),
        "preferred_net_margin": float(t["preferred_net_margin"]),
        "bid_ready_profit_floor_usd": float(t["minimum_net_profit_usd"]),
        "personal_cash_required_max_usd": float(t["personal_cash_required_max_usd"]),
        "minimum_evidence_confidence": "B",
        "minimum_completeness_for_approval": 80.0,
        "note": t["note"],
        "default_min_margin": float(DEFAULT_MIN_MARGIN),
        "default_min_profit_usd": float(DEFAULT_MIN_PROFIT_USD),
        "readiness_classes": [PROVEN, VERIFY, REJECT],
    }


def load_targets_from_store() -> dict[str, Any]:
    return owner_targets()
