"""Configurable owner profit targets — dollar profit matters more than % margin."""

from __future__ import annotations

import json
import os
from typing import Any


# Canonical owner-facing profit states
PROVEN_PROFITABLE = "PROVEN_PROFITABLE"
LIKELY_PROFITABLE = "LIKELY_PROFITABLE"
POSSIBLE_PROFIT = "POSSIBLE_PROFIT"
UNPROVEN = "UNPROVEN"
UNPROFITABLE = "UNPROFITABLE"
EXECUTION_BLOCKED = "EXECUTION_BLOCKED"

# Price-basis proof signals
PROFITABLE_AT_PUBLIC_RETAIL = "PROFITABLE_AT_PUBLIC_RETAIL"
PROFITABLE_AT_QUOTED_COST = "PROFITABLE_AT_QUOTED_COST"
PROFITABLE_AT_DISTRIBUTOR_COST = "PROFITABLE_AT_DISTRIBUTOR_COST"
PROFITABLE_WITH_PERMITTED_EQUAL = "PROFITABLE_WITH_PERMITTED_EQUAL"

BUILD = "20261002-profit-first-economics-routing"


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def owner_targets(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Owner-configurable thresholds. Large $ profit can pass at lower margin %."""
    ov = overrides or {}
    min_profit = ov.get("minimum_expected_profit_usd")
    if min_profit is None:
        min_profit = _f("M3_MIN_EXPECTED_PROFIT_USD", 5000.0)
    min_post = ov.get("minimum_post_financing_profit_usd")
    if min_post is None:
        min_post = _f("M3_MIN_POST_FINANCING_PROFIT_USD", 2500.0)
    min_margin = ov.get("minimum_margin_pct")
    if min_margin is None:
        raw = (os.getenv("M3_MIN_MARGIN_PCT") or "").strip()
        min_margin = float(raw) if raw else None  # optional — None means not enforced
    min_conf = ov.get("minimum_evidence_confidence") or (os.getenv("M3_MIN_EVIDENCE_CONFIDENCE") or "B").strip().upper()
    min_complete = ov.get("minimum_completeness_for_approval")
    if min_complete is None:
        min_complete = _f("M3_MIN_COMPLETENESS_FOR_APPROVAL", 80.0)
    # Bid-ready floor (separate from surfacing) — preserve economic_integrity default
    try:
        from economic_integrity import min_actual_profit_usd

        bid_floor = float(min_actual_profit_usd())
    except Exception:
        bid_floor = _f("AI_MIN_ACTUAL_PROFIT_USD", 10000.0)

    return {
        "minimum_expected_profit_usd": float(min_profit),
        "minimum_post_financing_profit_usd": float(min_post),
        "minimum_margin_pct": float(min_margin) if min_margin is not None else None,
        "minimum_evidence_confidence": min_conf,
        "minimum_completeness_for_approval": float(min_complete),
        "bid_ready_profit_floor_usd": bid_floor,
        "note": (
            "Dollar profit targets drive surfacing. Margin % is optional. "
            "Bid-ready floor is separate from research/surfacing thresholds."
        ),
    }


def load_targets_from_store() -> dict[str, Any]:
    """Optional persisted owner overrides under M3_DATA_ROOT."""
    try:
        from m3_data_root import data_path

        path = data_path("m3_owner_profit_targets.json")
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return owner_targets(data.get("targets") or data)
    except Exception:
        pass
    return owner_targets()
