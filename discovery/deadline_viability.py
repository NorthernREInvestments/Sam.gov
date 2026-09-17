"""Deadline runway + viability — first-class product-resale pursuit logic.

Launch policy:
  <3 calendar days  → TOO_LATE (default: no paid research / normal pursuit)
  3–<5 days         → RUSH (may proceed; deadline-risk treatment)
  5–<14 days        → GOOD (preferred normal window)
  14+ days          → PLENTY_OF_TIME
  unparseable/none  → UNKNOWN → NEEDS_DEADLINE_REVIEW

Deadline urgency never overrides failed deal qualification.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from application_clock import now_utc as app_now_utc
from discovery.deadline import normalize_deadline

# --- Viability states ---
VIABILITY_TOO_LATE = "TOO_LATE"
VIABILITY_RUSH = "RUSH"
VIABILITY_GOOD = "GOOD"
VIABILITY_PLENTY = "PLENTY_OF_TIME"
VIABILITY_UNKNOWN = "UNKNOWN"

BUCKET_NEEDS_DEADLINE_REVIEW = "NEEDS_DEADLINE_REVIEW"
BUCKET_NORMAL_QUEUE = "NORMAL_ACTIONABLE"
BUCKET_TOO_LATE = "TOO_LATE"
BUCKET_RUSH_EXCEPTION = "RUSH_EXCEPTION"

# Pursuit floors (calendar days)
HARD_PURSUIT_FLOOR_DAYS = 3
PREFERRED_PURSUIT_FLOOR_DAYS = 5
PLENTY_THRESHOLD_DAYS = 14

# Display badges
BADGE_RUSH = "RUSH"
BADGE_GOOD = "GOOD"
BADGE_PLENTY = "PLENTY OF TIME"
BADGE_TOO_LATE = "TOO LATE"
BADGE_UNKNOWN = "DEADLINE UNKNOWN"
BADGE_EXPIRED = "EXPIRED"

# Action urgency levels
URGENCY_CRITICAL = "CRITICAL"
URGENCY_HIGH = "HIGH"
URGENCY_NORMAL = "NORMAL"

_ACTIONABLE = frozenset({VIABILITY_RUSH, VIABILITY_GOOD, VIABILITY_PLENTY})


def classify_deadline_viability(
    calendar_days: int | None,
    *,
    expired: bool = False,
) -> str:
    """
    Deterministic boundary rules (calendar days remaining):
      expired or < 0 → TOO_LATE
      < 3            → TOO_LATE
      3, 4           → RUSH   (exactly 3 = RUSH)
      5 .. 13        → GOOD   (exactly 5 = GOOD)
      >= 14          → PLENTY_OF_TIME (exactly 14 = PLENTY)
      None           → UNKNOWN
    """
    if calendar_days is None:
        return VIABILITY_UNKNOWN
    if expired or calendar_days < 0:
        return VIABILITY_TOO_LATE
    if calendar_days < HARD_PURSUIT_FLOOR_DAYS:
        return VIABILITY_TOO_LATE
    if calendar_days < PREFERRED_PURSUIT_FLOOR_DAYS:
        return VIABILITY_RUSH
    if calendar_days < PLENTY_THRESHOLD_DAYS:
        return VIABILITY_GOOD
    return VIABILITY_PLENTY


def _parse_dt(value: datetime | date | str | None) -> datetime | date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        if len(s) == 10 and s[4] == "-":
            return date.fromisoformat(s)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_deadline_runway(
    *,
    response_deadline: datetime | date | str | None = None,
    deadline_raw: str | None = None,
    deadline_timezone: str | None = None,
    deadline_tz_confidence: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Compute runway_days / runway_hours and viability.

    Uses solicitation timezone when KNOWN. Never silently assumes UTC/local
    as source truth when timezone is unknown.
    """
    now_utc = now or app_now_utc()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    tz_known = (deadline_tz_confidence or "").upper() == "KNOWN" and bool(deadline_timezone)
    raw_text = deadline_raw

    # Prefer explicit aware datetime
    dl_obj = _parse_dt(response_deadline)
    parsed_local: datetime | None = None
    deadline_aware: datetime | None = None
    date_only = False
    precision = "UNKNOWN"

    if isinstance(dl_obj, datetime):
        if dl_obj.tzinfo is not None:
            deadline_aware = dl_obj
            parsed_local = dl_obj.astimezone(dl_obj.tzinfo).replace(tzinfo=None)
            tz_known = True
            deadline_timezone = deadline_timezone or str(dl_obj.tzinfo)
            precision = "DATETIME_TZ"
        else:
            parsed_local = dl_obj
            precision = "DATETIME_NAIVE"
    elif isinstance(dl_obj, date):
        parsed_local = datetime(dl_obj.year, dl_obj.month, dl_obj.day, 23, 59, 59)
        date_only = True
        precision = "DATE_ONLY"
    elif deadline_raw:
        norm = normalize_deadline(
            deadline_raw,
            timezone_hint=deadline_timezone,
            timezone_explicit=tz_known,
        )
        raw_text = norm.get("deadline_raw") or deadline_raw
        if not tz_known:
            tz_known = (norm.get("timezone_confidence") or "").upper() == "KNOWN" and bool(norm.get("timezone"))
            if tz_known:
                deadline_timezone = norm.get("timezone")
        if norm.get("utc_deadline"):
            deadline_aware = datetime.fromisoformat(norm["utc_deadline"])
            precision = "DATETIME_TZ" if tz_known else "DATETIME_CONVERTED"
        if norm.get("parsed_local"):
            parsed_local = datetime.fromisoformat(norm["parsed_local"])
            # Detect date-only sources (defaulted 17:00 from normalize without time in raw)
            if not re_has_time(deadline_raw):
                date_only = True
                precision = "DATE_ONLY"
            elif not tz_known:
                precision = "DATETIME_TZ_UNCERTAIN"
        if norm.get("parsed_local") is None and not deadline_aware:
            return _unknown_result(raw_text, reason="unparseable_deadline")

    if deadline_aware is None and parsed_local is None:
        return _unknown_result(raw_text, reason="no_deadline")

    # Build aware deadline when TZ known
    if deadline_aware is None and parsed_local is not None and tz_known and deadline_timezone and ZoneInfo:
        try:
            deadline_aware = parsed_local.replace(tzinfo=ZoneInfo(deadline_timezone))
            precision = "DATE_ONLY" if date_only else "DATETIME_TZ"
        except Exception:
            tz_known = False
            precision = "DATETIME_TZ_UNCERTAIN"

    expired = False
    runway_hours: float | None = None
    calendar_days: int | None = None

    if deadline_aware is not None:
        if deadline_aware.tzinfo is None:
            deadline_aware = deadline_aware.replace(tzinfo=timezone.utc)
        delta = deadline_aware - now_utc
        runway_hours = round(delta.total_seconds() / 3600.0, 3)
        expired = delta.total_seconds() < 0
        # Calendar days in deadline's timezone
        try:
            tz = deadline_aware.tzinfo
            now_local = now_utc.astimezone(tz)
            dl_local = deadline_aware.astimezone(tz)
            calendar_days = (dl_local.date() - now_local.date()).days
        except Exception:
            calendar_days = int(delta.total_seconds() // 86400)
    else:
        # TZ unknown — calendar compare only; no fake UTC precision
        assert parsed_local is not None
        today = now_utc.date()  # operational "today" in UTC date — flagged uncertain
        calendar_days = (parsed_local.date() - today).days
        expired = calendar_days < 0
        # Hours only as coarse estimate from noon/EOD local vs now UTC — mark uncertain
        if date_only:
            # Use end-of-day local as operational bound without claiming TZ certainty
            eod = datetime(parsed_local.year, parsed_local.month, parsed_local.day, 23, 59, 59)
            runway_hours = round((eod - now_utc.replace(tzinfo=None)).total_seconds() / 3600.0, 3)
            precision = "DATE_ONLY"
        else:
            runway_hours = round(
                (parsed_local - now_utc.replace(tzinfo=None)).total_seconds() / 3600.0, 3
            )
            precision = "DATETIME_TZ_UNCERTAIN"

    viability = classify_deadline_viability(calendar_days, expired=expired)
    actionable = viability in _ACTIONABLE
    display = format_runway_display(
        calendar_days=calendar_days,
        runway_hours=runway_hours,
        viability=viability,
        expired=expired,
        timezone_known=tz_known,
    )

    return {
        "deadline_runway_days": calendar_days,
        "deadline_runway_hours": runway_hours,
        "deadline_viability": viability,
        "deadline_actionable": actionable,
        "deadline_expired": expired,
        "deadline_timezone": deadline_timezone if tz_known else None,
        "deadline_timezone_known": tz_known,
        "deadline_tz_confidence": "KNOWN" if tz_known else "UNKNOWN",
        "raw_deadline_text": raw_text,
        "deadline_precision": precision,
        "deadline_display": display["label"],
        "deadline_badge": display["badge"],
        "queue_bucket": (
            BUCKET_NEEDS_DEADLINE_REVIEW
            if viability == VIABILITY_UNKNOWN
            else (BUCKET_TOO_LATE if viability == VIABILITY_TOO_LATE else BUCKET_NORMAL_QUEUE)
        ),
        "hard_pursuit_floor_days": HARD_PURSUIT_FLOOR_DAYS,
        "preferred_pursuit_floor_days": PREFERRED_PURSUIT_FLOOR_DAYS,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def re_has_time(raw: str | None) -> bool:
    if not raw:
        return False
    import re

    return bool(re.search(r"\d{1,2}:\d{2}|T\d{2}", raw))


def _unknown_result(raw_text: str | None, *, reason: str) -> dict[str, Any]:
    return {
        "deadline_runway_days": None,
        "deadline_runway_hours": None,
        "deadline_viability": VIABILITY_UNKNOWN,
        "deadline_actionable": False,
        "deadline_expired": False,
        "deadline_timezone": None,
        "deadline_timezone_known": False,
        "deadline_tz_confidence": "UNKNOWN",
        "raw_deadline_text": raw_text,
        "deadline_precision": "UNKNOWN",
        "deadline_display": "DEADLINE UNKNOWN",
        "deadline_badge": BADGE_UNKNOWN,
        "queue_bucket": BUCKET_NEEDS_DEADLINE_REVIEW,
        "reason": reason,
        "hard_pursuit_floor_days": HARD_PURSUIT_FLOOR_DAYS,
        "preferred_pursuit_floor_days": PREFERRED_PURSUIT_FLOOR_DAYS,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def format_runway_display(
    *,
    calendar_days: int | None,
    runway_hours: float | None,
    viability: str,
    expired: bool,
    timezone_known: bool,
) -> dict[str, str]:
    if viability == VIABILITY_UNKNOWN:
        return {"label": "DEADLINE UNKNOWN", "badge": BADGE_UNKNOWN}
    if expired or (calendar_days is not None and calendar_days < 0):
        return {"label": "EXPIRED", "badge": BADGE_EXPIRED}
    if calendar_days == 0 and runway_hours is not None and 0 <= runway_hours < 24:
        hrs = max(1, int(runway_hours)) if runway_hours >= 1 else 0
        if hrs == 0 and runway_hours > 0:
            return {"label": "DUE IN <1 HOUR", "badge": BADGE_TOO_LATE}
        label = f"DUE IN {hrs} HOUR{'S' if hrs != 1 else ''}"
        # Don't fake TZ precision in label when unknown
        if not timezone_known:
            label = label + " (TZ UNCERTAIN)"
        return {"label": label, "badge": BADGE_TOO_LATE}
    if calendar_days is None:
        return {"label": "DEADLINE UNKNOWN", "badge": BADGE_UNKNOWN}
    label = f"DUE IN {calendar_days} DAY{'S' if calendar_days != 1 else ''}"
    if not timezone_known:
        label = label + " (TZ UNCERTAIN)"
    badge = {
        VIABILITY_RUSH: BADGE_RUSH,
        VIABILITY_GOOD: BADGE_GOOD,
        VIABILITY_PLENTY: BADGE_PLENTY,
        VIABILITY_TOO_LATE: BADGE_TOO_LATE,
    }.get(viability, BADGE_UNKNOWN)
    return {"label": label, "badge": badge}


def enrich_opportunity_deadline(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Attach runway/viability fields onto an opportunity dict (non-destructive copy)."""
    out = dict(row)
    runway = compute_deadline_runway(
        response_deadline=row.get("response_deadline"),
        deadline_raw=row.get("deadline_raw") or row.get("raw_deadline_text"),
        deadline_timezone=row.get("deadline_timezone"),
        deadline_tz_confidence=row.get("deadline_tz_confidence") or row.get("deadline_timezone_confidence"),
        now=now,
    )
    out.update(runway)
    # Honor explicit manual override already on row
    ov = row.get("deadline_manual_override") or (row.get("raw_metadata") or {}).get("deadline_manual_override")
    if isinstance(ov, dict) and ov.get("active"):
        out["deadline_manual_override"] = ov
        out["queue_bucket"] = BUCKET_RUSH_EXCEPTION
        out["deadline_override_active"] = True
    else:
        out["deadline_override_active"] = False
    return out


def record_manual_deadline_override(
    *,
    reason: str,
    actor: str,
    at: datetime | None = None,
    allow_paid_research: bool = False,
    allow_normal_queue: bool = True,
) -> dict[str, Any]:
    """Explicit auditable override for TOO_LATE / exceptional pursuit."""
    if not reason or not str(reason).strip():
        raise ValueError("manual override requires a non-empty reason")
    if not actor or not str(actor).strip():
        raise ValueError("manual override requires an actor")
    ts = at or app_now_utc()
    return {
        "active": True,
        "reason": str(reason).strip(),
        "actor": str(actor).strip(),
        "at": ts.isoformat(),
        "allow_paid_research": bool(allow_paid_research),
        "allow_normal_queue": bool(allow_normal_queue),
        "kind": "DEADLINE_MANUAL_OVERRIDE",
        "auditable": True,
    }


def clear_manual_deadline_override(*, actor: str, reason: str = "cleared") -> dict[str, Any]:
    return {
        "active": False,
        "cleared_by": actor,
        "reason": reason,
        "at": app_now_utc().isoformat(),
        "auditable": True,
    }


def may_trigger_paid_research(
    viability: str | None,
    *,
    deal_qualified: bool = True,
    manual_override: dict[str, Any] | None = None,
    research_kind: str = "generic",
) -> dict[str, Any]:
    """
    Reusable gate for OpenAI / USAspending / supplier / financing / commercial research.
    Does not call any paid API.
    """
    v = (viability or VIABILITY_UNKNOWN).upper()
    ov = manual_override or {}
    override_ok = bool(ov.get("active") and ov.get("allow_paid_research"))

    if not deal_qualified:
        return {
            "allowed": False,
            "reason": "deal_not_qualified",
            "viability": v,
            "research_kind": research_kind,
            "note": "Deadline urgency cannot override failed deal qualification",
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    if v == VIABILITY_TOO_LATE and not override_ok:
        return {
            "allowed": False,
            "reason": "too_late_default_block",
            "viability": v,
            "research_kind": research_kind,
            "override_required": True,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    if v == VIABILITY_UNKNOWN and not override_ok:
        return {
            "allowed": False,
            "reason": "unknown_deadline_block",
            "viability": v,
            "research_kind": research_kind,
            "override_required": True,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    if v == VIABILITY_RUSH:
        return {
            "allowed": True,
            "reason": "rush_allowed_when_qualified",
            "viability": v,
            "research_kind": research_kind,
            "deadline_risk": True,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    if v in {VIABILITY_GOOD, VIABILITY_PLENTY}:
        return {
            "allowed": True,
            "reason": "normal_window",
            "viability": v,
            "research_kind": research_kind,
            "deadline_risk": False,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    # TOO_LATE / UNKNOWN with override
    if override_ok:
        return {
            "allowed": True,
            "reason": "manual_override",
            "viability": v,
            "research_kind": research_kind,
            "override": ov,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
        }

    return {
        "allowed": False,
        "reason": "not_allowed",
        "viability": v,
        "research_kind": research_kind,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
    }


def may_enter_normal_pursuit_queue(
    viability: str | None,
    *,
    deal_qualified: bool = True,
    manual_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """TOO_LATE excluded from normal queue unless explicit override."""
    if not deal_qualified:
        return {
            "allowed": False,
            "reason": "deal_not_qualified",
            "bucket": None,
            "note": "Deadline cannot salvage a failed deal",
        }
    v = (viability or VIABILITY_UNKNOWN).upper()
    ov = manual_override or {}
    if v == VIABILITY_UNKNOWN:
        return {"allowed": False, "reason": "needs_deadline_review", "bucket": BUCKET_NEEDS_DEADLINE_REVIEW}
    if v == VIABILITY_TOO_LATE:
        if ov.get("active") and ov.get("allow_normal_queue"):
            return {"allowed": True, "reason": "manual_override", "bucket": BUCKET_RUSH_EXCEPTION}
        return {"allowed": False, "reason": "too_late", "bucket": BUCKET_TOO_LATE}
    return {"allowed": True, "reason": "actionable", "bucket": BUCKET_NORMAL_QUEUE, "viability": v}


def action_urgency_for_unresolved(
    *,
    viability: str | None = None,
    runway_days: int | None = None,
    blocker: str = "generic",
    deal_state: str | None = None,
) -> dict[str, Any]:
    """
    Escalate unresolved execution dependencies under deadline pressure.
    Does not create a parallel task system — returns urgency for existing actions.
    """
    v = (viability or "").upper()
    days = runway_days
    if days is None and v == VIABILITY_UNKNOWN:
        return {"urgency": URGENCY_HIGH, "reason": "deadline_unknown_resolve_first", "blocker": blocker}

    critical_blockers = {
        "supplier_quote_missing",
        "funding_unresolved",
        "document_unresolved",
        "bid_ready_submit",
        "required_document",
    }
    if days is not None and days < 0:
        return {"urgency": URGENCY_CRITICAL, "reason": "expired", "blocker": blocker}
    if days is not None and days <= 1:
        if deal_state == "BID_READY" or blocker == "bid_ready_submit":
            return {
                "urgency": URGENCY_CRITICAL,
                "reason": "CRITICAL: REVIEW/SUBMIT",
                "blocker": blocker,
            }
        return {"urgency": URGENCY_CRITICAL, "reason": "deadline_1_day_or_less", "blocker": blocker}
    if days is not None and days <= HARD_PURSUIT_FLOOR_DAYS:
        if blocker in critical_blockers or True:
            return {"urgency": URGENCY_CRITICAL, "reason": "rush_floor_unresolved", "blocker": blocker}
    if days is not None and days < PREFERRED_PURSUIT_FLOOR_DAYS + 2:  # ~6 days band
        if days <= 6 and blocker in {"supplier_quote_missing", "funding_unresolved", "document_unresolved"}:
            return {"urgency": URGENCY_HIGH, "reason": "approaching_preferred_floor", "blocker": blocker}
    if v == VIABILITY_RUSH:
        return {"urgency": URGENCY_HIGH, "reason": "rush_window", "blocker": blocker}
    return {"urgency": URGENCY_NORMAL, "reason": "normal_window", "blocker": blocker}


def _due_sort_key(row: dict[str, Any]) -> tuple:
    """Ascending due: earlier first. Missing due sorts last within bucket."""
    rd = row.get("response_deadline") or row.get("deadline_raw")
    dt = _parse_dt(rd)
    if isinstance(dt, datetime):
        return (0, dt.timestamp() if dt.tzinfo else dt.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(dt, date):
        return (0, datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp())
    # Try runway enrichment fields
    days = row.get("deadline_runway_days")
    if isinstance(days, int):
        return (0, float(days))
    return (1, float("inf"))


def sort_operator_queue(
    opportunities: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    include_too_late: bool = False,
    include_unknown_in_normal: bool = False,
) -> dict[str, Any]:
    """
    Default operator ordering for qualified opportunities:
      1) actionable viability (RUSH, GOOD, PLENTY)
      2) due date ASC
      3) research_priority DESC
      4) estimated/actual profit DESC when tied
    TOO_LATE excluded from normal queue unless include_too_late / override.
    UNKNOWN → NEEDS_DEADLINE_REVIEW (not silently buried).
    DUE_TODAY / DUE_WITHIN_24_HOURS prominently surfaced when enrichable.
    """
    from deadline_runtime import (
        STATUS_DEADLINE_CONFLICT,
        STATUS_DUE_TODAY,
        STATUS_DUE_WITHIN_24_HOURS,
        STATUS_EXPIRED,
        evaluate_deadline,
    )

    now_resolved = now or app_now_utc()
    enriched = [enrich_opportunity_deadline(o, now=now_resolved) for o in opportunities]
    normal: list[dict[str, Any]] = []
    needs_deadline: list[dict[str, Any]] = []
    too_late: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    urgent_surface: list[dict[str, Any]] = []
    deadline_conflicts: list[dict[str, Any]] = []
    expired_historical: list[dict[str, Any]] = []

    viability_rank = {VIABILITY_RUSH: 0, VIABILITY_GOOD: 1, VIABILITY_PLENTY: 2}

    for row in enriched:
        # Attach runtime evaluation when evidence or deadline present
        runtime = evaluate_deadline(
            response_deadline=row.get("response_deadline") or row.get("operational_deadline") or row.get("deadline_raw"),
            deadline_timezone=row.get("deadline_timezone"),
            deadline_evidence=row.get("deadline_evidence"),
            portal_status=row.get("portal_status"),
            now=now_resolved,
        )
        row["deadline_runtime"] = runtime
        row["deadline_status"] = runtime.get("deadline_status")
        row["deadline_confidence"] = runtime.get("deadline_confidence")
        if runtime.get("deadline_status") == STATUS_EXPIRED:
            row["deadline_viability"] = VIABILITY_TOO_LATE
            row["deadline_actionable"] = False
            expired_historical.append(row)
            continue
        if runtime.get("deadline_status") == STATUS_DEADLINE_CONFLICT:
            deadline_conflicts.append(row)
            needs_deadline.append(row)
            continue
        if runtime.get("deadline_status") in {STATUS_DUE_TODAY, STATUS_DUE_WITHIN_24_HOURS}:
            row["urgent_surface"] = True
            urgent_surface.append(row)

        v = row.get("deadline_viability") or VIABILITY_UNKNOWN
        ov = row.get("deadline_manual_override") if isinstance(row.get("deadline_manual_override"), dict) else {}
        pursuit = may_enter_normal_pursuit_queue(v, deal_qualified=not row.get("deal_failed"), manual_override=ov)

        if row.get("deal_failed"):
            too_late.append(row)
            row["queue_bucket"] = "DEAL_FAILED"
            continue

        if v == VIABILITY_UNKNOWN:
            needs_deadline.append(row)
            continue
        if v == VIABILITY_TOO_LATE:
            if ov.get("active") and ov.get("allow_normal_queue"):
                exceptions.append(row)
            else:
                too_late.append(row)
                expired_historical.append(row)
            continue
        if pursuit.get("allowed"):
            normal.append(row)
        else:
            too_late.append(row)

    def sort_key(r: dict[str, Any]) -> tuple:
        v = r.get("deadline_viability") or VIABILITY_UNKNOWN
        rank = viability_rank.get(v, 99)
        # Urgent first within normal
        urgent_rank = 0 if r.get("urgent_surface") else 1
        due = _due_sort_key(r)
        score = -(r.get("research_priority") or 0)
        profit = r.get("estimated_profit") or r.get("actual_profit") or r.get("profit_usd")
        try:
            profit_key = -float(profit) if profit is not None else 0.0
        except (TypeError, ValueError):
            profit_key = 0.0
        return (urgent_rank, rank, due, score, profit_key, r.get("id") or r.get("external_id") or "")

    normal_sorted = sorted(normal, key=sort_key)
    exceptions_sorted = sorted(exceptions, key=sort_key)
    needs_sorted = sorted(needs_deadline, key=lambda r: (_due_sort_key(r), -(r.get("research_priority") or 0)))
    too_late_sorted = sorted(too_late, key=sort_key)
    urgent_sorted = sorted(urgent_surface, key=sort_key)
    conflict_sorted = sorted(deadline_conflicts, key=sort_key)

    urgent_ids = {id(r) for r in urgent_sorted}
    return {
        "normal_queue": normal_sorted,
        "rush_exceptions": exceptions_sorted,
        "needs_deadline_review": needs_sorted,
        "too_late": [r for r in too_late_sorted if r.get("queue_bucket") != "DEAL_FAILED"],
        "deal_failed": [r for r in too_late_sorted if r.get("queue_bucket") == "DEAL_FAILED"],
        "urgent_surface": urgent_sorted,
        "expired_historical": expired_historical,
        "deadline_conflict_flagged": conflict_sorted,
        "ordered_for_operator": urgent_sorted
        + [r for r in normal_sorted if id(r) not in urgent_ids]
        + (exceptions_sorted if include_too_late else [])
        + (needs_sorted if include_unknown_in_normal else [])
        + conflict_sorted,
        "counts": {
            "normal": len(normal_sorted),
            "rush_exceptions": len(exceptions_sorted),
            "needs_deadline_review": len(needs_sorted),
            "too_late": len([r for r in too_late_sorted if r.get("queue_bucket") != "DEAL_FAILED"]),
            "urgent": len(urgent_sorted),
            "expired_historical": len(expired_historical),
            "deadline_conflict": len(conflict_sorted),
        },
        "evaluated_at": now_resolved.isoformat(),
        "clock_mode": __import__("application_clock", fromlist=["clock_mode"]).clock_mode(),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def apply_deadline_to_action_priority(
    base_priority: int,
    *,
    viability: str | None,
    runway_days: int | None,
    blocker: str = "generic",
) -> dict[str, Any]:
    """Adjust existing next-action priority scores (lower = more urgent)."""
    urg = action_urgency_for_unresolved(
        viability=viability, runway_days=runway_days, blocker=blocker
    )
    adj = base_priority
    if urg["urgency"] == URGENCY_CRITICAL:
        adj = min(adj, 8)
    elif urg["urgency"] == URGENCY_HIGH:
        adj = min(adj, 25)
    return {"priority": adj, "urgency": urg["urgency"], "reason": urg["reason"], "base_priority": base_priority}
