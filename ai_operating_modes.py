"""AI operating mode configuration — LEAN default, no AI calls in this build."""

from __future__ import annotations

import os
from typing import Any

MODE_LEAN = "LEAN"
MODE_GROWTH = "GROWTH"
MODE_MAXIMUM = "MAXIMUM"

SUPPORTED_MODES = frozenset({MODE_LEAN, MODE_GROWTH, MODE_MAXIMUM})


def get_operating_mode() -> str:
    raw = (os.getenv("AI_OPERATING_MODE") or MODE_LEAN).strip().upper()
    return raw if raw in SUPPORTED_MODES else MODE_LEAN


def mode_config(mode: str | None = None) -> dict[str, Any]:
    m = (mode or get_operating_mode()).upper()
    if m not in SUPPORTED_MODES:
        m = MODE_LEAN
    presets = {
        MODE_LEAN: {
            "model_tier": "economy",
            "research_depth": "minimal",
            "ai_candidate_limit": 2,
            "auto_transcript_analysis": False,
            "second_pass_review": False,
            "bid_review_depth": "standard",
            "proactive_monitoring_hours": 24,
            "monthly_philosophy_usd": 40,
        },
        MODE_GROWTH: {
            "model_tier": "balanced",
            "research_depth": "moderate",
            "ai_candidate_limit": 5,
            "auto_transcript_analysis": True,
            "second_pass_review": True,
            "bid_review_depth": "deep",
            "proactive_monitoring_hours": 12,
            "monthly_philosophy_usd": 120,
        },
        MODE_MAXIMUM: {
            "model_tier": "premium",
            "research_depth": "extensive",
            "ai_candidate_limit": 10,
            "auto_transcript_analysis": True,
            "second_pass_review": True,
            "bid_review_depth": "exhaustive",
            "proactive_monitoring_hours": 4,
            "monthly_philosophy_usd": 400,
        },
    }
    return {"mode": m, **presets[m], "LIVE_API_REQUESTS": 0, "OpenAI": 0}
