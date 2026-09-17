"""Discovery scheduling foundation — not activated on app startup / tests."""

from __future__ import annotations

from typing import Any

from discovery.constants import CADENCE_FAST, CADENCE_NORMAL, CADENCE_SLOW

SCHEDULE_TIERS = {
    CADENCE_FAST: {"runs_per_day": 4, "description": "Cheap/allowed sources multiple times/day"},
    CADENCE_NORMAL: {"runs_per_day": 1, "description": "Daily sync"},
    CADENCE_SLOW: {"runs_per_day": 0.5, "description": "Fragile sources less frequently"},
}


def scheduling_plan(sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe future recurring discovery — does not start background jobs."""
    by_tier = {CADENCE_FAST: [], CADENCE_NORMAL: [], CADENCE_SLOW: []}
    for s in sources or []:
        meta = s.get("metadata_json") or {}
        cadence = meta.get("cadence") or CADENCE_NORMAL
        if cadence not in by_tier:
            cadence = CADENCE_NORMAL
        if s.get("enabled") and s.get("adapter_status") == "IMPLEMENTED":
            by_tier[cadence].append(s.get("source_id"))
    return {
        "tiers": SCHEDULE_TIERS,
        "queued_by_tier": by_tier,
        "activated": False,
        "note": "No background network calls from ordinary application startup",
        "LIVE_API_REQUESTS": 0,
    }
