"""Per-source discovery checkpoints — bootstrap vs incremental."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from national_discovery_constants import (
    CYCLE_COMPLETE,
    CYCLE_INCOMPLETE,
    DEFAULT_OVERLAP_HOURS,
    MODE_BOOTSTRAP,
    MODE_INCREMENTAL,
)


def _utc() -> str:
    return now_utc().isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def determine_discovery_mode(source: dict[str, Any], *, force_bootstrap: bool = False) -> str:
    if force_bootstrap:
        return MODE_BOOTSTRAP
    if not source.get("last_successful_checkpoint"):
        return MODE_BOOTSTRAP
    return MODE_INCREMENTAL


def incremental_window(
    source: dict[str, Any],
    *,
    now: datetime | None = None,
    overlap_hours: int | None = None,
) -> dict[str, Any]:
    """
    Resume from last SUCCESSFUL checkpoint with safe overlap.
    Failed attempts never move the successful cursor.
    """
    now = now or now_utc()
    overlap = overlap_hours if overlap_hours is not None else int(source.get("overlap_hours") or DEFAULT_OVERLAP_HOURS)
    success = _parse_ts(source.get("last_successful_checkpoint"))
    if success is None:
        return {
            "mode": MODE_BOOTSTRAP,
            "since": None,
            "overlap_hours": overlap,
            "resume_from": None,
            "note": "no_successful_checkpoint_bootstrap_required",
        }
    since = success - timedelta(hours=overlap)
    return {
        "mode": MODE_INCREMENTAL,
        "since": since.isoformat(),
        "overlap_hours": overlap,
        "resume_from": success.isoformat(),
        "last_attempted": source.get("last_attempted_checkpoint"),
        "note": "prefer_duplicate_work_over_missing_records",
    }


def filter_records_for_incremental(
    records: list[dict[str, Any]],
    *,
    since: str | None,
    timestamp_fields: tuple[str, ...] = (
        "source_modified_at",
        "modified_at",
        "updated_at",
        "posted_at",
        "created_at",
        "source_created_at",
    ),
) -> list[dict[str, Any]]:
    """Keep records newer than since (inclusive overlap). Missing timestamps are kept (safe)."""
    if not since:
        return list(records)
    since_dt = _parse_ts(since)
    if since_dt is None:
        return list(records)
    out = []
    for r in records:
        ts = None
        for f in timestamp_fields:
            ts = _parse_ts(r.get(f))
            if ts:
                break
        if ts is None or ts >= since_dt:
            out.append(r)
    return out


def begin_source_cycle(source_id: str, mode: str) -> dict[str, Any]:
    return {
        "cycle_id": f"CYC-{uuid4().hex[:12]}",
        "source_id": source_id,
        "mode": mode,
        "started_at": _utc(),
        "status": "IN_PROGRESS",
    }


def complete_source_cycle(
    cycle: dict[str, Any],
    *,
    success: bool,
    records_seen: int,
    budget_exhausted: bool = False,
) -> dict[str, Any]:
    out = dict(cycle)
    out["finished_at"] = _utc()
    out["success"] = success
    out["records_seen"] = records_seen
    if budget_exhausted or not success:
        out["status"] = CYCLE_INCOMPLETE if budget_exhausted else "FAILED"
        out["coverage_claim"] = "INCOMPLETE — do not pretend full coverage"
    else:
        out["status"] = CYCLE_COMPLETE
        out["coverage_claim"] = "COMPLETE_FOR_SOURCE_CYCLE"
    # Checkpoint string = finish time on success only (caller persists via registry)
    out["checkpoint_candidate"] = out["finished_at"] if success and not budget_exhausted else None
    return out


def catch_up_plan_after_failures(source: dict[str, Any]) -> dict[str, Any]:
    """
    Example: success Mon, fail Tue/Wed, work Thu → resume Monday success + overlap.
    """
    window = incremental_window(source)
    return {
        "kind": "CatchUpPlan",
        "source_id": source.get("source_id"),
        "resume_from_successful_checkpoint": source.get("last_successful_checkpoint"),
        "ignored_failed_attempts": source.get("last_attempted_checkpoint"),
        "window": window,
        "rule": "failed_source_must_not_advance_successful_checkpoint",
    }
