"""AuthoritativeFreshnessGate — foundation for READY_FOR_SUBMISSION."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc
from national_discovery_constants import (
    DEFAULT_FRESHNESS_HOURS_DEADLINE_CRITICAL,
    DEFAULT_FRESHNESS_HOURS_NORMAL,
    MON_DEADLINE_CRITICAL,
    NOT_READY_STALE,
    NOT_READY_UNREVIEWED_CHANGE,
    READY_FOR_SUBMISSION,
    SEV_CRITICAL,
    SEV_MATERIAL,
    SEV_UNKNOWN,
)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def freshness_window_hours(*, monitoring_tier: str | None = None, config: dict[str, Any] | None = None) -> float:
    cfg = config or {}
    if monitoring_tier == MON_DEADLINE_CRITICAL:
        return float(cfg.get("freshness_hours_deadline_critical") or DEFAULT_FRESHNESS_HOURS_DEADLINE_CRITICAL)
    return float(cfg.get("freshness_hours_normal") or DEFAULT_FRESHNESS_HOURS_NORMAL)


def evaluate_authoritative_freshness_gate(
    *,
    last_authoritative_check_at: str | None,
    current_version_confirmed: bool,
    amendment_set_confirmed: bool,
    qa_confirmed: bool = True,
    deadline_confirmed: bool = True,
    pricing_forms_confirmed: bool = True,
    required_forms_confirmed: bool = True,
    submission_instructions_confirmed: bool = True,
    unreviewed_material_changes: list[dict[str, Any]] | None = None,
    monitoring_tier: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Foundation only — does NOT submit bids.
    Blocks READY_FOR_SUBMISSION when stale or unreviewed material changes exist.
    """
    blockers = []
    unreviewed = [
        c
        for c in (unreviewed_material_changes or [])
        if c.get("severity") in {SEV_CRITICAL, SEV_MATERIAL, SEV_UNKNOWN} and not c.get("operator_reviewed")
    ]
    if unreviewed:
        blockers.append("unreviewed_material_or_critical_changes")

    window_h = freshness_window_hours(monitoring_tier=monitoring_tier, config=config)
    checked = _parse(last_authoritative_check_at)
    now = now_utc()
    if checked is None:
        blockers.append("no_authoritative_check")
        stale = True
    else:
        stale = (now - checked) > timedelta(hours=window_h)
        if stale:
            blockers.append("authoritative_check_stale")

    checks = {
        "current_solicitation_version": current_version_confirmed,
        "current_amendment_set": amendment_set_confirmed,
        "qa_addenda": qa_confirmed,
        "deadline": deadline_confirmed,
        "pricing_forms": pricing_forms_confirmed,
        "required_forms": required_forms_confirmed,
        "submission_instructions": submission_instructions_confirmed,
    }
    for name, ok in checks.items():
        if not ok:
            blockers.append(f"unconfirmed_{name}")

    if blockers:
        readiness = NOT_READY_UNREVIEWED_CHANGE if unreviewed else NOT_READY_STALE
        return {
            "kind": "AuthoritativeFreshnessGate",
            "passed": False,
            "readiness": readiness,
            "blockers": blockers,
            "freshness_window_hours": window_h,
            "last_authoritative_check_at": last_authoritative_check_at,
            "stale": stale if checked else True,
            "would_allow_ready_for_submission": False,
            "bid_submitted": False,
        }

    return {
        "kind": "AuthoritativeFreshnessGate",
        "passed": True,
        "readiness": READY_FOR_SUBMISSION,
        "blockers": [],
        "freshness_window_hours": window_h,
        "last_authoritative_check_at": last_authoritative_check_at,
        "stale": False,
        "would_allow_ready_for_submission": True,
        "bid_submitted": False,
        "note": "Gate foundation only — no bid submission in DEVELOPMENT_NO_OUTREACH",
    }
