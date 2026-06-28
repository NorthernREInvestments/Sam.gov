"""Two-step contract screening: text-first, then full PDF analysis if score >= threshold."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from models import Contract

SKIP_LOW_SCORE_LABEL = "Skipped — Low Score"


def full_analysis_min_score() -> int:
    raw = os.getenv("FULL_ANALYSIS_MIN_SCORE", "6").strip()
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 6


def analysis_stage(analysis: dict[str, Any] | None) -> str | None:
    if not isinstance(analysis, dict):
        return None
    return analysis.get("screening_stage")


def is_full_analysis_complete(analysis: dict[str, Any] | None) -> bool:
    return analysis_stage(analysis) == "full"


def text_score_from_analysis(analysis: dict[str, Any] | None) -> int | None:
    if not isinstance(analysis, dict):
        return None
    for key in ("text_score", "score"):
        val = analysis.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def qualifies_for_full_analysis(analysis: dict[str, Any] | None, *, force: bool = False) -> bool:
    if force:
        return True
    if is_full_analysis_complete(analysis):
        return False
    score = text_score_from_analysis(analysis)
    return score is not None and score >= full_analysis_min_score()


def needs_text_screening(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return True
    return analysis_stage(analysis) not in ("text", "full")


def needs_intake(row: Contract, *, force: bool = False) -> bool:
    if force:
        return True
    analysis = row.analysis if isinstance(row.analysis, dict) else None
    if needs_text_screening(analysis):
        return True
    if qualifies_for_full_analysis(analysis):
        return True
    return False


def mark_low_text_score(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "text"
    analysis["skip_reason"] = SKIP_LOW_SCORE_LABEL
    analysis["pursue"] = False
    row.analysis = analysis
    row.status = "skipped"
    row.last_updated_at = datetime.now(timezone.utc)


def mark_pending_full_analysis(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "text"
    analysis.pop("skip_reason", None)
    if row.status in (None, "new", "skipped"):
        row.status = "reviewing"
    row.analysis = analysis
    row.last_updated_at = datetime.now(timezone.utc)


def finalize_full_analysis(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "full"
    analysis.pop("skip_reason", None)
    if analysis.get("text_score") is None and analysis.get("score") is not None:
        analysis["text_score"] = analysis.get("score")
    row.analysis = analysis
    row.last_updated_at = datetime.now(timezone.utc)
    if analysis.get("pursue") is False:
        row.status = "skipped"
    elif row.status in (None, "new", "skipped"):
        row.status = "reviewing"
