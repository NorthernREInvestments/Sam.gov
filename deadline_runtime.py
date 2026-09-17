"""Standard timezone-aware deadline evaluation on top of ApplicationClock.

Does not invent timezones or invent midnight for date-only deadlines.
Integrates deadline-conflict provenance and portal-status stale protection.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from application_clock import get_clock, now_utc
from deadline_conflict import reconcile_deadline_evidence
from discovery.deadline_viability import (
    VIABILITY_GOOD,
    VIABILITY_PLENTY,
    VIABILITY_RUSH,
    VIABILITY_TOO_LATE,
    VIABILITY_UNKNOWN,
    classify_deadline_viability,
)

# --- Deadline status (runtime) ---
STATUS_OPEN = "OPEN"
STATUS_DUE_TODAY = "DUE_TODAY"
STATUS_DUE_WITHIN_24_HOURS = "DUE_WITHIN_24_HOURS"
STATUS_EXPIRED = "EXPIRED"
STATUS_DEADLINE_UNKNOWN = "DEADLINE_UNKNOWN"
STATUS_DEADLINE_CONFLICT = "DEADLINE_CONFLICT"

# Queue surface buckets
QUEUE_ACTIONABLE = "ACTIONABLE"
QUEUE_URGENT = "URGENT_SURFACE"
QUEUE_EXPIRED_HISTORICAL = "EXPIRED_HISTORICAL"
QUEUE_DEADLINE_REVIEW = "DEADLINE_REVIEW"
QUEUE_DEADLINE_CONFLICT = "DEADLINE_CONFLICT_FLAGGED"

_TZ_ABBREV = {
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "CT": "America/Chicago",
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "ET": "America/New_York",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "MT": "America/Denver",
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
}


def resolve_iana_timezone(label: str | None) -> tuple[str | None, str]:
    """
    Map a procurement timezone label to IANA when evidence supports it.
    Returns (iana_or_none, confidence KNOWN|UNKNOWN).
    Never invents a timezone when evidence does not establish one.
    """
    if not label:
        return None, "UNKNOWN"
    s = str(label).strip()
    if "/" in s:
        try:
            ZoneInfo(s)
            return s, "KNOWN"
        except Exception:
            return None, "UNKNOWN"
    upper = s.upper()
    if upper in _TZ_ABBREV:
        return _TZ_ABBREV[upper], "KNOWN"
    return None, "UNKNOWN"


def _parse_time_12h(time_s: str | None) -> tuple[int, int] | None:
    if not time_s:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_s.strip(), re.I)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    ampm = m.group(3).upper()
    if ampm == "PM" and hh < 12:
        hh += 12
    if ampm == "AM" and hh == 12:
        hh = 0
    return hh, mm


def parse_procurement_deadline(
    raw: str | datetime | date | None,
    *,
    timezone_label: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """
    Parse a deadline preserving uncertainty for missing time/timezone.
    Does NOT invent 00:00 / 23:59 when only a date is known.
    """
    if raw is None or raw == "":
        return {
            "ok": False,
            "raw": None,
            "date": None,
            "time": None,
            "timezone_label": None,
            "iana_timezone": None,
            "timezone_confidence": "UNKNOWN",
            "deadline_at": None,
            "date_only": False,
            "time_unknown": True,
            "role": role,
        }

    if isinstance(raw, datetime):
        if raw.tzinfo is not None:
            local = raw.astimezone(raw.tzinfo).replace(tzinfo=None)
            iana, conf = resolve_iana_timezone(timezone_label or str(raw.tzinfo))
            if conf != "KNOWN":
                conf = "KNOWN"  # offset-aware datetime establishes usable instant
            return {
                "ok": True,
                "raw": raw.isoformat(),
                "date": local.date().isoformat(),
                "time": local.strftime("%H:%M"),
                "timezone_label": timezone_label or str(raw.tzinfo),
                "iana_timezone": iana,
                "timezone_confidence": conf,
                "deadline_at": raw,
                "date_only": False,
                "time_unknown": False,
                "role": role,
                "parsed_local": local,
            }
        local = raw
        iana, conf = resolve_iana_timezone(timezone_label)
        deadline_at = None
        if iana and conf == "KNOWN":
            deadline_at = local.replace(tzinfo=ZoneInfo(iana))
        return {
            "ok": True,
            "raw": raw.isoformat(),
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M"),
            "timezone_label": timezone_label,
            "iana_timezone": iana,
            "timezone_confidence": conf,
            "deadline_at": deadline_at,
            "date_only": False,
            "time_unknown": deadline_at is None,
            "role": role,
            "parsed_local": local,
        }

    if isinstance(raw, date) and not isinstance(raw, datetime):
        return {
            "ok": True,
            "raw": raw.isoformat(),
            "date": raw.isoformat(),
            "time": None,
            "timezone_label": timezone_label,
            "iana_timezone": resolve_iana_timezone(timezone_label)[0],
            "timezone_confidence": resolve_iana_timezone(timezone_label)[1],
            "deadline_at": None,
            "date_only": True,
            "time_unknown": True,
            "role": role,
            "parsed_local": None,
        }

    s = re.sub(r"\s+", " ", str(raw)).strip()
    # MM/DD/YYYY[, ] h:mm AM/PM TZ
    m = re.search(
        r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})(?:[,\s]+(\d{1,2}:\d{2}\s*[AP]M))?\s*([A-Z]{2,5})?",
        s,
        re.I,
    )
    date_s = None
    time_s = None
    tz_label = timezone_label
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        date_s = f"{y:04d}-{mo:02d}-{d:02d}"
        time_s = (m.group(4) or "").strip() or None
        if m.group(5):
            tz_label = tz_label or m.group(5).strip().upper()
    else:
        m2 = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?(?:\s+([A-Z]{2,5}))?",
            s,
            re.I,
        )
        if not m2:
            return {
                "ok": False,
                "raw": s,
                "date": None,
                "time": None,
                "timezone_label": tz_label,
                "iana_timezone": None,
                "timezone_confidence": "UNKNOWN",
                "deadline_at": None,
                "date_only": False,
                "time_unknown": True,
                "role": role,
            }
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        }
        mo = months[m2.group(1).lower()]
        d = int(m2.group(2))
        y = int(m2.group(3))
        date_s = f"{y:04d}-{mo:02d}-{d:02d}"
        time_s = (m2.group(4) or "").strip() or None
        if m2.group(5):
            tz_label = tz_label or m2.group(5).strip().upper()

    assert date_s is not None
    y, mo, d = (int(x) for x in date_s.split("-"))
    iana, tz_conf = resolve_iana_timezone(tz_label)
    hm = _parse_time_12h(time_s)
    date_only = hm is None
    deadline_at: datetime | None = None
    parsed_local: datetime | None = None
    if hm is not None:
        parsed_local = datetime(y, mo, d, hm[0], hm[1], 0)
        if iana and tz_conf == "KNOWN":
            deadline_at = parsed_local.replace(tzinfo=ZoneInfo(iana))
        # else: time known, timezone unknown — do not invent UTC midnight
    return {
        "ok": True,
        "raw": s,
        "date": date_s,
        "time": time_s,
        "timezone_label": tz_label,
        "iana_timezone": iana,
        "timezone_confidence": tz_conf if tz_label else "UNKNOWN",
        "deadline_at": deadline_at,
        "date_only": date_only,
        "time_unknown": date_only,
        "role": role,
        "parsed_local": parsed_local,
    }


def apply_amendments(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Mark superseded CLOSE deadlines when an authoritative amendment exists.
    Preserves all evidence; does not delete.
    """
    out = [dict(r) for r in records]
    amendments = [
        r
        for r in out
        if (r.get("role") or "").upper() in {"AMENDMENT", "AMENDED_CLOSE", "CLOSE_AMENDMENT"}
        or r.get("is_amendment") is True
    ]
    if not amendments:
        return out
    # Prefer authoritative amendment
    auth_am = [
        r
        for r in amendments
        if (r.get("evidence_class") or "").startswith("AUTHORITATIVE")
        or r.get("authoritative_for_bidding")
    ]
    winner = auth_am[0] if auth_am else amendments[-1]
    winner_date = (winner.get("parsed") or {}).get("date") or parse_procurement_deadline(winner.get("value")).get("date")
    for r in out:
        if r is winner or r.get("value") == winner.get("value"):
            r["superseded"] = False
            r["operational_close_candidate"] = True
            continue
        role = (r.get("role") or "").upper()
        if role in {"CLOSE", "AMENDMENT", "AMENDED_CLOSE", "CLOSE_AMENDMENT", "MIRROR_CLAIMED_DEADLINE"}:
            r_date = (r.get("parsed") or {}).get("date")
            if r_date and winner_date and r_date != winner_date:
                r["superseded"] = True
                r["superseded_by"] = winner.get("value")
                r["operational_close_candidate"] = False
    return out


def evaluate_deadline(
    *,
    response_deadline: str | datetime | date | None = None,
    deadline_timezone: str | None = None,
    deadline_evidence: list[dict[str, Any]] | None = None,
    portal_status: str | None = None,
    local_timezone: str = "America/Chicago",
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Standard deadline evaluation result.
    Uses ApplicationClock when now is omitted.
    Portal OPEN/ACTIVE cannot override an expired authoritative deadline.
    """
    clock = get_clock()
    evaluated_at = now or clock.now_utc()
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)

    current_local = evaluated_at.astimezone(ZoneInfo(local_timezone))
    records = apply_amendments(list(deadline_evidence or []))
    # Do not let superseded closes create false unresolved conflicts
    active_records = [r for r in records if not r.get("superseded")]
    reconciliation = reconcile_deadline_evidence(active_records) if active_records else None
    if reconciliation is not None:
        reconciliation = {**reconciliation, "records": records}  # preserve full provenance including superseded

    # Prefer CLOSE from evidence / reconciliation over OPEN
    operational_raw = None
    conflict = False
    conflict_resolved = True
    deadline_confidence = "UNKNOWN"
    open_raw = None
    close_raw = None

    if reconciliation:
        operational_raw = reconciliation.get("operational_deadline")
        conflict = bool(reconciliation.get("conflict"))
        conflict_resolved = bool(reconciliation.get("conflict_resolved", True))
        if reconciliation.get("status") == "DEADLINE_CONFLICT_UNRESOLVED":
            conflict_resolved = False
        for r in records:
            if r.get("role") == "OPEN":
                open_raw = open_raw or r.get("value")
            if r.get("role") == "CLOSE" and not r.get("superseded"):
                close_raw = close_raw or r.get("value")
                if r.get("authoritative_for_bidding") or (r.get("evidence_class") or "").startswith("AUTHORITATIVE"):
                    deadline_confidence = "HIGH"
                    if not response_deadline:
                        operational_raw = operational_raw or r.get("value")
        # OPEN vs CLOSE alone is not an unresolved conflict
        if open_raw and close_raw and conflict_resolved:
            conflict = conflict  # keep informational flag from reconciliation

    if response_deadline and not operational_raw:
        operational_raw = response_deadline
        deadline_confidence = "MEDIUM"

    if operational_raw is None and close_raw:
        operational_raw = close_raw

    parsed = parse_procurement_deadline(
        operational_raw,
        timezone_label=deadline_timezone,
        role="CLOSE",
    )
    open_parsed = parse_procurement_deadline(open_raw, role="OPEN") if open_raw else None

    # Unresolved competing closes
    if reconciliation and not conflict_resolved:
        status = STATUS_DEADLINE_CONFLICT
        deadline_confidence = "LOW"
    elif not parsed.get("ok"):
        status = STATUS_DEADLINE_UNKNOWN
        deadline_confidence = "UNKNOWN"
    else:
        status = STATUS_OPEN  # provisional; refined below
        if parsed.get("timezone_confidence") == "KNOWN" and not parsed.get("time_unknown"):
            deadline_confidence = max_conf(deadline_confidence, "HIGH")
        elif parsed.get("date_only") or parsed.get("time_unknown"):
            deadline_confidence = "MEDIUM" if parsed.get("date") else "LOW"
            if deadline_confidence == "UNKNOWN":
                deadline_confidence = "MEDIUM"

    deadline_at: datetime | None = parsed.get("deadline_at")
    time_remaining = None
    calendar_days: int | None = None
    expired = False
    time_uncertain = bool(parsed.get("time_unknown") or parsed.get("date_only"))

    if deadline_at is not None:
        delta = deadline_at - evaluated_at
        time_remaining = {
            "total_seconds": delta.total_seconds(),
            "hours": round(delta.total_seconds() / 3600.0, 3),
            "display": _format_remaining(delta.total_seconds()),
        }
        expired = delta.total_seconds() < 0
        try:
            tz = deadline_at.tzinfo or ZoneInfo(local_timezone)
            calendar_days = (deadline_at.astimezone(tz).date() - evaluated_at.astimezone(tz).date()).days
        except Exception:
            calendar_days = int(delta.total_seconds() // 86400)
    elif parsed.get("ok") and parsed.get("date"):
        # Date known, time unknown — do not invent clock time; use date comparison only
        dl_date = date.fromisoformat(parsed["date"])
        today = current_local.date()
        calendar_days = (dl_date - today).days
        expired = calendar_days < 0
        time_remaining = {
            "total_seconds": None,
            "hours": None,
            "calendar_days": calendar_days,
            "display": f"{calendar_days} calendar day(s) (time UNKNOWN)",
            "time_unknown": True,
        }
        if calendar_days == 0:
            # Preserve uncertainty near deadline
            status = STATUS_DUE_TODAY
            deadline_confidence = "LOW"
        elif not conflict_resolved:
            status = STATUS_DEADLINE_CONFLICT

    # Status refinement when precise
    if parsed.get("ok") and status not in {STATUS_DEADLINE_UNKNOWN, STATUS_DEADLINE_CONFLICT}:
        if expired:
            status = STATUS_EXPIRED
        elif deadline_at is not None:
            secs = (deadline_at - evaluated_at).total_seconds()
            if secs < 0:
                status = STATUS_EXPIRED
            elif secs <= 24 * 3600:
                # same calendar day?
                tz = deadline_at.tzinfo or ZoneInfo(local_timezone)
                if evaluated_at.astimezone(tz).date() == deadline_at.astimezone(tz).date():
                    status = STATUS_DUE_TODAY
                else:
                    status = STATUS_DUE_WITHIN_24_HOURS
            else:
                tz = deadline_at.tzinfo or ZoneInfo(local_timezone)
                if evaluated_at.astimezone(tz).date() == deadline_at.astimezone(tz).date():
                    status = STATUS_DUE_TODAY
                else:
                    status = STATUS_OPEN
        elif calendar_days is not None:
            if calendar_days < 0:
                status = STATUS_EXPIRED
            elif calendar_days == 0:
                status = STATUS_DUE_TODAY
            else:
                status = STATUS_OPEN

    if reconciliation and not conflict_resolved:
        status = STATUS_DEADLINE_CONFLICT

    # Stale portal protection: portal OPEN cannot override EXPIRED
    portal_retained = portal_status
    portal_overridden = False
    if portal_status and str(portal_status).upper() in {
        "OPEN", "ACTIVE", "CURRENT", "ACCEPTING RESPONSES", "ACCEPTING_RESPONSES",
    }:
        if status == STATUS_EXPIRED:
            portal_overridden = True

    viability = classify_deadline_viability(calendar_days, expired=status == STATUS_EXPIRED)
    if status == STATUS_DEADLINE_UNKNOWN:
        viability = VIABILITY_UNKNOWN
    if status == STATUS_DEADLINE_CONFLICT:
        # Conservative: treat viability from earliest operational date already in calendar_days
        if calendar_days is None:
            viability = VIABILITY_UNKNOWN

    actionable = status not in {
        STATUS_EXPIRED,
        STATUS_DEADLINE_UNKNOWN,
        STATUS_DEADLINE_CONFLICT,
    } and viability in {VIABILITY_RUSH, VIABILITY_GOOD, VIABILITY_PLENTY}

    # Conflict / unknown not silently actionable
    if status == STATUS_DEADLINE_CONFLICT:
        actionable = False
    if status == STATUS_EXPIRED:
        actionable = False
        viability = VIABILITY_TOO_LATE

    # Urgent surface but research gate separate
    urgent = status in {STATUS_DUE_TODAY, STATUS_DUE_WITHIN_24_HOURS}
    enough_time_for_bid_ready = viability in {VIABILITY_GOOD, VIABILITY_PLENTY} and not urgent
    if urgent and viability == VIABILITY_RUSH:
        enough_time_for_bid_ready = False  # time risk — avoid expensive research by default

    queue_bucket = QUEUE_ACTIONABLE
    if status == STATUS_EXPIRED:
        queue_bucket = QUEUE_EXPIRED_HISTORICAL
    elif status == STATUS_DEADLINE_CONFLICT:
        queue_bucket = QUEUE_DEADLINE_CONFLICT
    elif status == STATUS_DEADLINE_UNKNOWN:
        queue_bucket = QUEUE_DEADLINE_REVIEW
    elif urgent:
        queue_bucket = QUEUE_URGENT
    elif not actionable:
        queue_bucket = QUEUE_EXPIRED_HISTORICAL if viability == VIABILITY_TOO_LATE else QUEUE_DEADLINE_REVIEW

    portal_registration_temporally_viable = status not in {STATUS_EXPIRED} and (
        status != STATUS_DEADLINE_UNKNOWN
    )

    return {
        "evaluated_at": evaluated_at.isoformat(),
        "clock_mode": clock.mode,
        "current_date_local": current_local.date().isoformat(),
        "current_datetime_local": current_local.isoformat(),
        "local_timezone": local_timezone,
        "open_date": open_raw,
        "close_date": close_raw or (operational_raw if parsed.get("role") != "OPEN" else None),
        "operational_deadline": operational_raw,
        "deadline_at": deadline_at.isoformat() if deadline_at else None,
        "deadline_timezone": parsed.get("timezone_label") or parsed.get("iana_timezone"),
        "deadline_timezone_iana": parsed.get("iana_timezone"),
        "timezone_confidence": parsed.get("timezone_confidence"),
        "date_only": parsed.get("date_only"),
        "time_unknown": parsed.get("time_unknown") or time_uncertain,
        "time_remaining": time_remaining,
        "calendar_days_remaining": calendar_days,
        "deadline_status": status,
        "deadline_confidence": deadline_confidence,
        "deadline_viability": viability,
        "actionable": actionable,
        "urgent_surface": urgent,
        "enough_time_for_bid_ready": enough_time_for_bid_ready,
        "avoid_expensive_research": urgent and not enough_time_for_bid_ready,
        "queue_bucket": queue_bucket,
        "portal_status_evidence": portal_retained,
        "portal_status_overridden_by_clock": portal_overridden,
        "portal_registration_temporally_viable": portal_registration_temporally_viable,
        "conflict": conflict,
        "conflict_resolved": conflict_resolved,
        "reconciliation": reconciliation,
        "open_parsed": open_parsed,
        "close_parsed": parsed,
        "deadline_evidence": records,
        "open_is_not_response_deadline": True if open_raw else None,
    }


def max_conf(a: str, b: str) -> str:
    order = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _format_remaining(seconds: float) -> str:
    if seconds < 0:
        return f"expired {abs(int(seconds))}s ago"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def classify_queue_opportunities(
    opportunities: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Apply deadline intelligence to transactional queues.
    Expired → EXPIRED_HISTORICAL (not deleted).
    DUE_TODAY / DUE_WITHIN_24_HOURS prominently surfaced.
    UNKNOWN → deadline review. CONFLICT prominently flagged.
    """
    evaluated_at = now or now_utc()
    actionable: list[dict[str, Any]] = []
    urgent: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for opp in opportunities:
        ev = evaluate_deadline(
            response_deadline=opp.get("response_deadline") or opp.get("operational_deadline"),
            deadline_timezone=opp.get("deadline_timezone"),
            deadline_evidence=opp.get("deadline_evidence"),
            portal_status=opp.get("portal_status"),
            now=evaluated_at,
        )
        row = {**opp, "deadline_evaluation": ev}
        for k in (
            "deadline_status",
            "deadline_viability",
            "actionable",
            "queue_bucket",
            "urgent_surface",
            "evaluated_at",
            "clock_mode",
            "time_remaining",
            "deadline_confidence",
            "portal_registration_temporally_viable",
        ):
            row[k] = ev.get(k)

        st = ev["deadline_status"]
        if st == STATUS_EXPIRED:
            expired.append(row)
        elif st == STATUS_DEADLINE_CONFLICT:
            conflicts.append(row)
        elif st == STATUS_DEADLINE_UNKNOWN:
            review.append(row)
        elif ev.get("urgent_surface"):
            urgent.append(row)
            if ev.get("actionable"):
                actionable.append(row)
        elif ev.get("actionable"):
            actionable.append(row)
        else:
            # TOO_LATE but not yet clock-expired, etc.
            if ev.get("deadline_viability") == VIABILITY_TOO_LATE:
                expired.append(row)
            else:
                review.append(row)

    return {
        "evaluated_at": evaluated_at.isoformat(),
        "clock_mode": get_clock().mode,
        "actionable_queue": actionable,
        "urgent_surface": urgent,
        "expired_historical": expired,
        "needs_deadline_review": review,
        "deadline_conflict_flagged": conflicts,
        "counts": {
            "actionable": len(actionable),
            "urgent": len(urgent),
            "expired_historical": len(expired),
            "needs_deadline_review": len(review),
            "deadline_conflict": len(conflicts),
        },
        # Confirmed expired must not remain in normal actionable queue
        "normal_actionable_excludes_expired": True,
    }
