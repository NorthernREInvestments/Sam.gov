"""Phase L.2.1 — deadline freshness before expensive enrichment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc

LIVE_ACTIONABLE = "LIVE_ACTIONABLE"
LIVE_DEADLINE_UNKNOWN = "LIVE_DEADLINE_UNKNOWN"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"
AWARDED = "AWARDED"
ARCHIVAL_RECORD = "ARCHIVAL_RECORD"


def parse_deadline(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s or s.upper() in {"UNKNOWN", "NONE", "N/A"}:
        return None
    # Common forms
    for candidate in (
        s,
        s.replace("Z", "+00:00"),
        s[:19] + "+00:00" if "T" in s and len(s) >= 19 else s,
    ):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # date-only
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def classify_deadline_freshness(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Compare solicitation deadline to now. Page 'OPEN' ≠ actionable."""
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    status_blob = " ".join(
        str(x or "")
        for x in (row.get("live_status"), row.get("status"), row.get("notice_status"), row.get("opportunity_status"))
    ).upper()
    if any(x in status_blob for x in ("CANCEL", "DELETED")):
        return {"deadline_state": CANCELLED, "deep_enrichment_allowed": False, "deadline": None}
    if "AWARD" in status_blob and "PRE-AWARD" not in status_blob:
        return {"deadline_state": AWARDED, "deep_enrichment_allowed": False, "deadline": None}

    deadline_raw = (
        row.get("response_deadline")
        or row.get("deadline")
        or row.get("responseDateActual")
        or row.get("responseDate")
    )
    deadline = parse_deadline(deadline_raw)
    if deadline is None:
        # Archival signal: listing says open but no deadline and old posted date
        posted = parse_deadline(row.get("posted_date") or row.get("publish_date") or row.get("postedDate"))
        if posted and (now - posted).days > 180:
            return {
                "deadline_state": ARCHIVAL_RECORD,
                "deep_enrichment_allowed": False,
                "deadline": None,
                "reason": "old_posting_no_deadline",
            }
        return {
            "deadline_state": LIVE_DEADLINE_UNKNOWN,
            "deep_enrichment_allowed": True,  # unknown ≠ expired
            "deadline": None,
            "deadline_raw": deadline_raw,
        }

    if deadline < now:
        return {
            "deadline_state": EXPIRED,
            "deep_enrichment_allowed": False,
            "deadline": deadline.isoformat(),
            "deadline_raw": deadline_raw,
            "reason": "deadline_before_now",
        }

    runway_hours = (deadline - now).total_seconds() / 3600.0
    return {
        "deadline_state": LIVE_ACTIONABLE,
        "deep_enrichment_allowed": True,
        "deadline": deadline.isoformat(),
        "deadline_raw": deadline_raw,
        "runway_hours": round(runway_hours, 2),
        "runway_days": round(runway_hours / 24.0, 2),
    }
