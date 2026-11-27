"""Canonical acquisition schedule policy (times + limits).

Free portals: twice daily (default 08:00 and 15:00 local).
Paid portals (NextBid): once daily at 08:00 local, capped by API call limits.

Does not enable the scheduler — only defines when runs are due.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_FREE_TIMES = ("08:00", "15:00")
DEFAULT_PAID_TIME = "08:00"
DEFAULT_TZ = "America/Denver"


def _parse_hhmm(token: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", token or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_time_list(raw: str | None, *, default: tuple[str, ...]) -> list[tuple[int, int]]:
    text = (raw or "").strip()
    parts = [p for p in re.split(r"[,;\s]+", text) if p] if text else list(default)
    if not parts:
        parts = list(default)
    out: list[tuple[int, int]] = []
    for p in parts:
        parsed = _parse_hhmm(p)
        if parsed and parsed not in out:
            out.append(parsed)
    if not out:
        for d in default:
            parsed = _parse_hhmm(d)
            if parsed:
                out.append(parsed)
    return out


def schedule_timezone() -> str:
    return (os.getenv("SCHEDULER_TIMEZONE") or DEFAULT_TZ).strip() or DEFAULT_TZ


def free_refresh_slots() -> list[tuple[int, int]]:
    """Local clock times for free-portal pulls (default 8am + 3pm)."""
    # Prefer FREE_REFRESH_TIMES; fall back to legacy single DAILY_REFRESH_* + 15:00
    raw = (os.getenv("FREE_REFRESH_TIMES") or "").strip()
    if raw:
        return parse_time_list(raw, default=DEFAULT_FREE_TIMES)
    hour = int(os.getenv("DAILY_REFRESH_HOUR") or "8")
    minute = int(os.getenv("DAILY_REFRESH_MINUTE") or "0")
    slots = [(hour, minute)]
    # Ensure afternoon slot exists when using legacy single-hour env
    if (15, 0) not in slots and hour != 15:
        slots.append((15, 0))
    return slots


def paid_refresh_slot() -> tuple[int, int]:
    """Local clock time for paid/NextBid daily pull (default 8am)."""
    raw = (os.getenv("PAID_REFRESH_TIME") or os.getenv("NEXTBID_REFRESH_TIME") or DEFAULT_PAID_TIME).strip()
    parsed = _parse_hhmm(raw)
    if parsed:
        return parsed
    hour = int(os.getenv("NEXTBID_REFRESH_HOUR") or os.getenv("DAILY_REFRESH_HOUR") or "8")
    minute = int(os.getenv("NEXTBID_REFRESH_MINUTE") or "0")
    return hour, minute


def slot_key(day: str, hour: int, minute: int) -> str:
    return f"{day}T{hour:02d}:{minute:02d}"


def due_free_slots(
    *,
    now_local: datetime,
    already_ran: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Which free slots are due now and not yet completed today."""
    already = already_ran or set()
    day = now_local.date().isoformat()
    due: list[dict[str, Any]] = []
    for hour, minute in free_refresh_slots():
        key = slot_key(day, hour, minute)
        reached = (now_local.hour, now_local.minute) >= (hour, minute)
        if reached and key not in already:
            due.append({"slot": key, "hour": hour, "minute": minute, "day": day})
    return due


def paid_due(
    *,
    now_local: datetime,
    last_run_day: str | None = None,
) -> dict[str, Any]:
    hour, minute = paid_refresh_slot()
    day = now_local.date().isoformat()
    key = slot_key(day, hour, minute)
    reached = (now_local.hour, now_local.minute) >= (hour, minute)
    already = last_run_day == day
    return {
        "slot": key,
        "hour": hour,
        "minute": minute,
        "day": day,
        "due": bool(reached and not already),
        "already_ran_today": already,
        "reached_clock": reached,
    }


def local_now(tz_name: str | None = None) -> datetime:
    name = tz_name or schedule_timezone()
    try:
        tz = ZoneInfo(name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    return datetime.now(tz)


def schedule_summary() -> dict[str, Any]:
    free = free_refresh_slots()
    paid_h, paid_m = paid_refresh_slot()
    return {
        "timezone": schedule_timezone(),
        "free_refresh_times": [f"{h:02d}:{m:02d}" for h, m in free],
        "paid_refresh_time": f"{paid_h:02d}:{paid_m:02d}",
        "free_note": "Full free-portal board + change-watch fingerprint pass each slot",
        "paid_note": "NextBid once daily at paid time, subject to API call limits + emergency lock",
        "scheduler_enabled_env": (os.getenv("SCHEDULER_ENABLED") or "").strip().lower()
        in {"1", "true", "yes"},
    }
