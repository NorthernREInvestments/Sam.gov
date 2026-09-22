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

# µLab funnel experiment thresholds (configurable — not FAR legal determinations)
MICRO_LAB_MAX_VALUE = Decimal(os.environ.get("MICRO_LAB_MAX_VALUE") or str(DEFAULT_SMALL_SA_MAX))
MICRO_LAB_MIN_VALUE = Decimal(os.environ.get("MICRO_LAB_MIN_VALUE") or "0")
MICRO_LAB_TARGET_RESULT_COUNT = int(os.environ.get("MICRO_LAB_TARGET_RESULT_COUNT") or "15")
MICRO_LAB_RAW_SEARCH_TARGET = int(os.environ.get("MICRO_LAB_RAW_SEARCH_TARGET") or "2000")
MICRO_LAB_MIN_DEADLINE_RUNWAY_DAYS = int(os.environ.get("MICRO_LAB_MIN_DEADLINE_RUNWAY_DAYS") or "2")


def threshold_config() -> dict[str, Any]:
    return {
        "micro_purchase_max_usd": str(DEFAULT_MICRO_MAX),
        "near_micro_max_usd": str(DEFAULT_NEAR_MICRO_MAX),
        "small_simplified_acq_max_usd": str(DEFAULT_SMALL_SA_MAX),
        "historical_market_window_days": HISTORICAL_MARKET_WINDOW_DAYS,
        "current_market_stale_days": CURRENT_MARKET_STALE_DAYS,
        "micro_lab_max_value": str(MICRO_LAB_MAX_VALUE),
        "micro_lab_min_value": str(MICRO_LAB_MIN_VALUE),
        "micro_lab_target_result_count": MICRO_LAB_TARGET_RESULT_COUNT,
        "micro_lab_raw_search_target": MICRO_LAB_RAW_SEARCH_TARGET,
        "micro_lab_min_deadline_runway_days": MICRO_LAB_MIN_DEADLINE_RUNWAY_DAYS,
        "note": "Configurable defaults — not a legal determination of FAR thresholds",
    }


def funnel_config() -> dict[str, Any]:
    return {
        "raw_search_target": MICRO_LAB_RAW_SEARCH_TARGET,
        "complete_target": MICRO_LAB_TARGET_RESULT_COUNT,
        "min_value": str(MICRO_LAB_MIN_VALUE),
        "max_value": str(MICRO_LAB_MAX_VALUE),
        "min_deadline_runway_days": MICRO_LAB_MIN_DEADLINE_RUNWAY_DAYS,
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
