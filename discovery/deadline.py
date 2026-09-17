"""Deadline / timezone normalization — never silently assume timezone."""

from __future__ import annotations
from application_clock import now_utc, today_local

import re
from datetime import date, datetime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def normalize_deadline(
    raw: str | date | datetime | None,
    *,
    timezone_hint: str | None = None,
    timezone_explicit: bool = False,
) -> dict[str, Any]:
    """
    Store raw, parsed local, timezone, UTC where resolvable.
    Unknown timezone is explicit — never silent default to ET/UTC for display as fact.
    """
    if raw is None or raw == "":
        return {
            "deadline_raw": None,
            "parsed_local": None,
            "timezone": None,
            "timezone_confidence": "UNKNOWN",
            "utc_deadline": None,
            "deadline_passed": False,
            "LIVE_API_REQUESTS": 0,
        }

    raw_s = raw.isoformat() if isinstance(raw, (date, datetime)) else str(raw).strip()
    tz_name = timezone_hint
    tz_conf = "KNOWN" if (timezone_explicit and timezone_hint) else "UNKNOWN"

    parsed_local: datetime | None = None
    utc_deadline: datetime | None = None

    if isinstance(raw, datetime):
        parsed_local = raw if raw.tzinfo is None else raw.replace(tzinfo=None)
        if raw.tzinfo is not None:
            utc_deadline = raw.astimezone(timezone.utc)
            tz_conf = "KNOWN"
            tz_name = tz_name or str(raw.tzinfo)
    elif isinstance(raw, date):
        parsed_local = datetime(raw.year, raw.month, raw.day, 17, 0, 0)
    else:
        # Try ISO-ish parse
        m = re.match(
            r"(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2}))?)?",
            raw_s,
        )
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh = int(m.group(4) or 17)
            mm = int(m.group(5) or 0)
            ss = int(m.group(6) or 0)
            parsed_local = datetime(y, mo, d, hh, mm, ss)
        else:
            # MM/DD/YYYY
            m2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw_s)
            if m2:
                mo, d, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
                parsed_local = datetime(y, mo, d, 17, 0, 0)
            else:
                # Month name: September 30, 2026 2:00 PM [Central]
                m3 = re.search(
                    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
                    r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})"
                    r"(?:\s+(\d{1,2}):(\d{2})\s*(AM|PM)?)?",
                    raw_s,
                    re.I,
                )
                if m3:
                    months = {
                        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                    }
                    mo = months[m3.group(1)[:3].lower()]
                    d = int(m3.group(2))
                    y = int(m3.group(3))
                    hh = int(m3.group(4) or 17)
                    mm = int(m3.group(5) or 0)
                    ampm = (m3.group(6) or "").upper()
                    if ampm == "PM" and hh < 12:
                        hh += 12
                    if ampm == "AM" and hh == 12:
                        hh = 0
                    parsed_local = datetime(y, mo, d, hh, mm, 0)
                    # Ambiguous zone labels alone never become KNOWN without explicit IANA hint
                    if re.search(r"\b(central|eastern|pacific|mountain|ct|et|pt|mt)\b", raw_s, re.I):
                        if not timezone_explicit:
                            tz_conf = "UNKNOWN"

    if parsed_local and tz_name and tz_conf == "KNOWN" and ZoneInfo is not None:
        try:
            local_aware = parsed_local.replace(tzinfo=ZoneInfo(tz_name))
            utc_deadline = local_aware.astimezone(timezone.utc)
        except Exception:
            tz_conf = "UNKNOWN"
            utc_deadline = None

    deadline_passed = False
    if utc_deadline:
        deadline_passed = utc_deadline < now_utc()
    elif parsed_local:
        # Compare date-only when TZ unknown — conservative date compare
        deadline_passed = parsed_local.date() < today_local()

    return {
        "deadline_raw": raw_s,
        "parsed_local": parsed_local.isoformat() if parsed_local else None,
        "timezone": tz_name if tz_conf == "KNOWN" else None,
        "timezone_confidence": tz_conf,
        "utc_deadline": utc_deadline.isoformat() if utc_deadline else None,
        "deadline_passed": deadline_passed,
        "note": "Timezone UNKNOWN unless source establishes it" if tz_conf == "UNKNOWN" else None,
        "LIVE_API_REQUESTS": 0,
    }
