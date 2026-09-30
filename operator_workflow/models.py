"""Projection result shape helpers."""

from __future__ import annotations

from typing import Any


def empty_projection() -> dict[str, Any]:
    return {
        "operator_workflow_state": "DISCOVERED",
        "operator_state_reason": "No source signals available",
        "operator_next_action": "Review the opportunity and confirm it is a tangible product fit.",
        "operator_blockers": [],
        "confidence_level": "UNKNOWN",
        "missing_information": [],
        "risk_flags": [],
        "state_conflict_detected": False,
        "conflicting_states": [],
        "source_signals": [],
        "build": None,
    }
