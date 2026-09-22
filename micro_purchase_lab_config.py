"""Configurable micro-purchase size bands — evidence classification only."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

# Federal-oriented defaults (USD). Override via env without code change.
DEFAULT_MICRO_MAX = Decimal(os.environ.get("M3_MICRO_PURCHASE_MAX_USD") or "10000")
DEFAULT_NEAR_MICRO_MAX = Decimal(os.environ.get("M3_NEAR_MICRO_MAX_USD") or "25000")
DEFAULT_SMALL_SA_MAX = Decimal(os.environ.get("M3_SMALL_SIMPLIFIED_ACQ_MAX_USD") or "250000")

CLASS_MICRO = "MICRO_PURCHASE"
CLASS_NEAR = "NEAR_MICRO"
CLASS_SMALL_SA = "SMALL_SIMPLIFIED_ACQUISITION"
CLASS_ABOVE = "ABOVE_MICRO_COMPARISON"
CLASS_UNKNOWN = "UNKNOWN"

HISTORICAL_MARKET_WINDOW_DAYS = int(os.environ.get("M3_HISTORICAL_MARKET_WINDOW_DAYS") or "90")
CURRENT_MARKET_STALE_DAYS = int(os.environ.get("M3_CURRENT_MARKET_STALE_DAYS") or "30")


def threshold_config() -> dict[str, Any]:
    return {
        "micro_purchase_max_usd": str(DEFAULT_MICRO_MAX),
        "near_micro_max_usd": str(DEFAULT_NEAR_MICRO_MAX),
        "small_simplified_acq_max_usd": str(DEFAULT_SMALL_SA_MAX),
        "historical_market_window_days": HISTORICAL_MARKET_WINDOW_DAYS,
        "current_market_stale_days": CURRENT_MARKET_STALE_DAYS,
        "note": "Configurable defaults — not a legal determination of FAR thresholds",
    }


def classify_opportunity_size(estimated_value: Any) -> str:
    """Map known dollar size to experiment band. Missing value → UNKNOWN."""
    amt = _as_money(estimated_value)
    if amt is None:
        return CLASS_UNKNOWN
    if amt < 0:
        return CLASS_UNKNOWN
    if amt <= DEFAULT_MICRO_MAX:
        return CLASS_MICRO
    if amt <= DEFAULT_NEAR_MICRO_MAX:
        return CLASS_NEAR
    if amt <= DEFAULT_SMALL_SA_MAX:
        return CLASS_SMALL_SA
    return CLASS_ABOVE


def is_micro_experiment_band(classification: str | None) -> bool:
    return str(classification or "") in {CLASS_MICRO, CLASS_NEAR, CLASS_SMALL_SA, CLASS_UNKNOWN}


def _as_money(value: Any) -> Decimal | None:
    if value is None or value == "" or str(value).upper() in {"UNKNOWN", "NONE", "NULL"}:
        return None
    try:
        raw = str(value).replace(",", "").replace("$", "").strip()
        if not raw:
            return None
        return Decimal(raw)
    except Exception:
        return None
