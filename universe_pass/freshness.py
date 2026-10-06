"""BidNet / source freshness states for the expanded live universe."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc

LIVE_CONFIRMED = "LIVE_CONFIRMED"
LIVE_PROBABLE = "LIVE_PROBABLE"
STALE = "STALE"
EXPIRED = "EXPIRED"
DUPLICATE = "DUPLICATE"
UNKNOWN_FRESHNESS = "UNKNOWN_FRESHNESS"


def _parse_dt(val: Any) -> datetime | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _deadline(rec: dict[str, Any]) -> datetime | None:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    for key in ("deadline", "response_deadline", "due_date", "close_date"):
        dt = _parse_dt(rec.get(key) or rr.get(key))
        if dt:
            return dt
    # deadline_raw may be a parseable string
    raw = rec.get("deadline_raw") or rr.get("deadline_raw")
    if raw:
        try:
            from discovery.deadline import normalize_deadline

            nd = normalize_deadline(str(raw))
            return _parse_dt(nd.get("utc_deadline") or nd.get("parsed_local") or nd.get("deadline_raw"))
        except Exception:
            pass
    return None


def _is_bidnet(rec: dict[str, Any]) -> bool:
    plat = str(rec.get("platform") or "").lower()
    sid = str((rec.get("row_ref") or {}).get("source_id") or "").lower() if isinstance(rec.get("row_ref"), dict) else ""
    prov = rec.get("source_provenance") or []
    if "bidnet" in plat or "bidnet" in sid:
        return True
    if isinstance(prov, list):
        for p in prov:
            if isinstance(p, dict) and "bidnet" in str(p.get("source_id") or "").lower():
                return True
    return False


def _status_dead(rec: dict[str, Any]) -> str | None:
    for key in ("source_status", "status", "live_status", "freshness"):
        st = str(rec.get(key) or "").upper()
        if st in {"EXPIRED", "CANCELLED", "CANCELED", "CLOSED", "CLOSED_EXPIRED", "AWARDED", "SUPERSEDED"}:
            return st
        rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
        st2 = str(rr.get(key) or "").upper()
        if st2 in {"EXPIRED", "CANCELLED", "CANCELED", "CLOSED", "AWARDED"}:
            return st2
    title = str(rec.get("title") or "").lower()
    if re.search(r"\b(cancel+ed|award\s+notice|closed\s+solicitation)\b", title):
        return "CANCELLED"
    return None


def assess_freshness(
    rec: dict[str, Any],
    *,
    harvested_at: datetime | None = None,
    harvest_max_age_hours: float = 72.0,
) -> dict[str, Any]:
    """Assign freshness state. Missing dates are NEVER LIVE_CONFIRMED."""
    now = now_utc()
    dead = _status_dead(rec)
    if dead in {"EXPIRED", "CLOSED", "CLOSED_EXPIRED", "AWARDED", "SUPERSEDED"}:
        return {
            "freshness_state": EXPIRED if dead != "CANCELLED" else STALE,
            "reason": f"status_{dead.lower()}",
            "deadline": None,
            "is_bidnet": _is_bidnet(rec),
            "actionable": False,
            "metadata_only": True,
        }
    if dead in {"CANCELLED", "CANCELED"}:
        return {
            "freshness_state": STALE,
            "reason": "cancelled",
            "deadline": None,
            "is_bidnet": _is_bidnet(rec),
            "actionable": False,
            "metadata_only": True,
        }

    deadline = _deadline(rec)
    bidnet = _is_bidnet(rec)
    url = rec.get("authoritative_url") or (rec.get("row_ref") or {}).get("detail_url")
    has_sol = bool(rec.get("solicitation_event_id") or (rec.get("row_ref") or {}).get("solicitation_number"))

    if deadline and deadline < now:
        return {
            "freshness_state": EXPIRED,
            "reason": "deadline_passed",
            "deadline": deadline.isoformat(),
            "is_bidnet": bidnet,
            "actionable": False,
            "metadata_only": bidnet,
        }

    if deadline and deadline >= now:
        return {
            "freshness_state": LIVE_CONFIRMED,
            "reason": "future_deadline",
            "deadline": deadline.isoformat(),
            "is_bidnet": bidnet,
            "actionable": True,
            "metadata_only": bidnet and not rec.get("attachments_metadata"),
        }

    # No deadline — BidNet open-bids harvest is probable if recent
    if bidnet:
        # Prefer updated_at as harvest proxy
        updated = _parse_dt(rec.get("updated_at") or harvested_at)
        age_ok = False
        if updated:
            age_ok = (now - updated) <= timedelta(hours=harvest_max_age_hours)
        if age_ok and (url or has_sol):
            return {
                "freshness_state": LIVE_PROBABLE,
                "reason": "bidnet_open_list_recent_no_deadline",
                "deadline": None,
                "is_bidnet": True,
                "actionable": bool(url),
                "metadata_only": True,
            }
        return {
            "freshness_state": UNKNOWN_FRESHNESS,
            "reason": "bidnet_missing_deadline",
            "deadline": None,
            "is_bidnet": True,
            "actionable": bool(url),
            "metadata_only": True,
        }

    # Non-BidNet without deadline
    if url or has_sol:
        return {
            "freshness_state": UNKNOWN_FRESHNESS,
            "reason": "missing_deadline",
            "deadline": None,
            "is_bidnet": False,
            "actionable": bool(url),
            "metadata_only": False,
        }
    return {
        "freshness_state": UNKNOWN_FRESHNESS,
        "reason": "insufficient_fields",
        "deadline": None,
        "is_bidnet": False,
        "actionable": False,
        "metadata_only": True,
    }


def amendment_or_duplicate_key(rec: dict[str, Any]) -> str | None:
    """Stable key for solicitation-level dedupe (amendments collapse)."""
    sol = str(
        rec.get("solicitation_event_id")
        or (rec.get("row_ref") or {}).get("solicitation_number")
        or (rec.get("row_ref") or {}).get("solicitation_id")
        or ""
    ).strip().lower()
    buyer = str(rec.get("buyer") or (rec.get("row_ref") or {}).get("agency") or "").strip().lower()
    if sol and sol not in {"none", "null", "n/a"} and buyer:
        return f"sol|{buyer}|{sol}"
    url = str(rec.get("authoritative_url") or (rec.get("row_ref") or {}).get("detail_url") or "").strip().lower()
    if url and ("/statewide/" in url or "solicitation" in url):
        m = re.search(r"/(\d{6,})/abstract", url)
        if m:
            return f"bnid|{m.group(1)}"
    return None
