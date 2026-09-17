"""Source health tracking + polite fetch policy."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from discovery.constants import (
    HEALTH_AUTH_REQUIRED,
    HEALTH_BLOCKED,
    HEALTH_BROKEN,
    HEALTH_DEGRADED,
    HEALTH_DISABLED,
    HEALTH_HEALTHY,
    HEALTH_UNTESTED,
)


DEFAULT_RATE_LIMIT = {
    "min_interval_seconds": 2.0,
    "max_requests_per_run": 20,
    "max_pages": 5,
    "timeout_seconds": 20,
    "max_retries": 1,
    "backoff_seconds": 5.0,
    "user_agent": "GovTrackerDiscovery/1.0 (+operator-controlled; respectful)",
}


def default_fetch_policy(**overrides: Any) -> dict[str, Any]:
    p = dict(DEFAULT_RATE_LIMIT)
    p.update(overrides)
    return p


def record_source_attempt(
    *,
    success: bool,
    records_seen: int = 0,
    records_added: int = 0,
    records_updated: int = 0,
    parse_warnings: int = 0,
    failure_type: str | None = None,
    prior_consecutive_failures: int = 0,
    enabled: bool = True,
) -> dict[str, Any]:
    """Compute updated health fields after an attempt. One broken source must not halt network."""
    now = now_utc()
    if not enabled:
        return {
            "health_status": HEALTH_DISABLED,
            "last_attempt": now.isoformat(),
            "consecutive_failures": prior_consecutive_failures,
            "LIVE_API_REQUESTS": 0,
        }

    if success:
        health = HEALTH_HEALTHY
        if parse_warnings > 0:
            health = HEALTH_DEGRADED
        return {
            "health_status": health,
            "last_attempt": now.isoformat(),
            "last_successful_sync": now.isoformat(),
            "last_failure": None,
            "failure_type": None,
            "consecutive_failures": 0,
            "records_seen": records_seen,
            "records_added": records_added,
            "records_updated": records_updated,
            "parse_warning_count": parse_warnings,
            "LIVE_API_REQUESTS": 0,
        }

    fails = prior_consecutive_failures + 1
    ft = failure_type or "UNKNOWN"
    if ft in {"AUTH_REQUIRED", "401", "403"}:
        health = HEALTH_AUTH_REQUIRED
    elif ft in {"BLOCKED", "CAPTCHA", "ROBOTS"}:
        health = HEALTH_BLOCKED
    elif fails >= 3:
        health = HEALTH_BROKEN
    else:
        health = HEALTH_DEGRADED

    return {
        "health_status": health,
        "last_attempt": now.isoformat(),
        "last_failure": now.isoformat(),
        "failure_type": ft,
        "consecutive_failures": fails,
        "records_seen": records_seen,
        "parse_warning_count": parse_warnings,
        "LIVE_API_REQUESTS": 0,
        "isolated": True,
        "note": "Source failure isolated — discovery network continues",
    }


def apply_health_to_source_row(row: Any, health: dict[str, Any]) -> None:
    row.health_status = health.get("health_status") or HEALTH_UNTESTED
    row.last_attempt = now_utc()
    if health.get("last_successful_sync"):
        row.last_successful_sync = now_utc()
        row.consecutive_failures = 0
        row.failure_type = None
    if health.get("last_failure"):
        row.last_failure = now_utc()
        row.consecutive_failures = health.get("consecutive_failures") or 0
        row.failure_type = health.get("failure_type")
    if "records_seen" in health:
        row.records_seen = (row.records_seen or 0) + int(health.get("records_seen") or 0)
    if "records_added" in health:
        row.records_added = (row.records_added or 0) + int(health.get("records_added") or 0)
    if "records_updated" in health:
        row.records_updated = (row.records_updated or 0) + int(health.get("records_updated") or 0)
    if "parse_warning_count" in health:
        row.parse_warning_count = (row.parse_warning_count or 0) + int(health.get("parse_warning_count") or 0)
