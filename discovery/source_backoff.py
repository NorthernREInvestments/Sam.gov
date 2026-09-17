"""Failure-aware discovery backoff — do not hammer persistent blockers every cycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc
from discovery.source_failure_taxonomy import BACKOFF_HOURS, REGISTRATION_REQUIRED, AUTH_REQUIRED, BOT_CHALLENGE, CAPTCHA


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


def should_skip_source_for_backoff(source_row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """
    Return skip=True when last failure is within backoff window.
    Registration/auth/bot use long intervals; never treat as silent omission — caller must account.
    """
    now = now or now_utc()
    failure = str(source_row.get("failure_class") or source_row.get("root_cause") or "").upper()
    last_fail = _parse_ts(source_row.get("last_failure_at") or source_row.get("backoff_until"))
    hours = float(source_row.get("backoff_hours") or BACKOFF_HOURS.get(failure, 0) or 0)
    if hours <= 0:
        return {"skip": False, "reason": None, "failure_class": failure or None}
    # Prefer explicit backoff_until
    until = _parse_ts(source_row.get("backoff_until"))
    if until is None and last_fail is not None:
        until = last_fail + timedelta(hours=hours)
    if until is None:
        return {"skip": False, "reason": None, "failure_class": failure or None}
    if now < until:
        return {
            "skip": True,
            "reason": "BACKOFF",
            "failure_class": failure or "UNKNOWN",
            "backoff_until": until.isoformat(),
            "accounted_state": failure
            if failure
            in {REGISTRATION_REQUIRED, AUTH_REQUIRED, BOT_CHALLENGE, CAPTCHA, "HTTP_403", "HTTP_429"}
            else "BACKOFF",
        }
    return {"skip": False, "reason": "backoff_expired", "failure_class": failure or None}


def compute_backoff_until(failure_class: str, *, now: datetime | None = None) -> str:
    now = now or now_utc()
    hours = float(BACKOFF_HOURS.get(failure_class, 6))
    return (now + timedelta(hours=hours)).isoformat()
