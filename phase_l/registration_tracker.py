"""Lightweight recurring buyer / portal registration tracker for Phase L.1.

Extends existing portal credential / coverage concepts without a new subsystem.
Does not auto-register (DEVELOPMENT_NO_OUTREACH).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

_ROOT = Path(__file__).resolve().parents[1]


def _tracker_path() -> Path:
    try:
        from m3_data_root import registration_tracker_path

        return registration_tracker_path()
    except Exception:
        return _ROOT / "data" / "buyer_portal_registration_tracker.json"


_TRACKER_PATH = _ROOT / "data" / "buyer_portal_registration_tracker.json"

NONE = "NONE"
EASY_REGISTRATION = "EASY_REGISTRATION"
REGISTRATION_WITH_DOCS = "REGISTRATION_WITH_DOCS"
LONG_LEAD_REGISTRATION = "LONG_LEAD_REGISTRATION"
RESTRICTIVE_ELIGIBILITY = "RESTRICTIVE_ELIGIBILITY"
UNKNOWN = "UNKNOWN"

ACTION_NONE = "NONE"
REGISTER_NOW = "REGISTER_NOW"
REGISTER_BEFORE_BID = "REGISTER_BEFORE_BID"
REGISTER_NOW_RECURRING_BUYER = "REGISTER_NOW_RECURRING_BUYER"
VERIFY_REGISTRATION_TIMING = "VERIFY_REGISTRATION_TIMING"
BLOCKED = "BLOCKED"

STATUS_NOT_REGISTERED = "NOT_REGISTERED"
STATUS_REGISTERED = "REGISTERED"
STATUS_UNKNOWN = "UNKNOWN"


def _utc() -> str:
    return now_utc().isoformat()


def default_tracker() -> dict[str, Any]:
    return {"kind": "BuyerPortalRegistrationTracker", "portals": {}, "updated_at": None}


def load_tracker(path: Path | None = None) -> dict[str, Any]:
    path = path or _tracker_path()
    if not path.exists():
        return default_tracker()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("portals"), dict):
            return data
    except Exception:
        pass
    return default_tracker()


def save_tracker(tracker: dict[str, Any], path: Path | None = None) -> Path:
    path = path or _tracker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(tracker)
    out["kind"] = "BuyerPortalRegistrationTracker"
    out["updated_at"] = _utc()
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return path


def get_portal_status(source_id: str, *, tracker: dict[str, Any] | None = None) -> dict[str, Any]:
    tracker = tracker or load_tracker()
    sid = str(source_id or "").strip()
    portals = tracker.get("portals") or {}
    row = portals.get(sid) if sid else None
    if not isinstance(row, dict):
        return {
            "source_id": sid,
            "registration_status": STATUS_UNKNOWN,
            "registration_type": UNKNOWN,
            "relevant_opportunities_seen": 0,
            "currently_live_relevant_opportunities": 0,
            "recommended_action": ACTION_NONE,
        }
    return dict(row)


def reset_live_counts(path: Path | None = None) -> dict[str, Any]:
    """Zero currently_live counters at the start of a hunt pass."""
    tracker = load_tracker(path)
    portals = tracker.get("portals") or {}
    for row in portals.values():
        if isinstance(row, dict):
            row["currently_live_relevant_opportunities"] = 0
    save_tracker(tracker, path)
    return tracker


def recurring_registration_actions(tracker: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    tracker = tracker or load_tracker()
    out: list[dict[str, Any]] = []
    for sid, row in (tracker.get("portals") or {}).items():
        if not isinstance(row, dict):
            continue
        action = str(row.get("recommended_action") or "")
        if action in {REGISTER_NOW_RECURRING_BUYER, REGISTER_BEFORE_BID, VERIFY_REGISTRATION_TIMING}:
            out.append({"source_id": sid, **row})
    return out


def record_portal_sighting(
    *,
    source_id: str,
    buyer_name: str | None = None,
    jurisdiction: str | None = None,
    registration_url: str | None = None,
    registration_type: str = EASY_REGISTRATION,
    is_relevant_product: bool = True,
    is_live: bool = True,
    opportunity_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Upsert sighting; recommend REGISTER_NOW_RECURRING_BUYER when repeated."""
    tracker = load_tracker(path)
    portals = tracker.setdefault("portals", {})
    sid = str(source_id or "").strip() or "unknown"
    row = portals.get(sid)
    if not isinstance(row, dict):
        row = {
            "buyer_id": sid,
            "buyer_name": buyer_name or sid,
            "jurisdiction": jurisdiction,
            "portal": sid,
            "registration_url": registration_url,
            "registration_type": registration_type,
            "registration_status": STATUS_NOT_REGISTERED,
            "relevant_opportunities_seen": 0,
            "currently_live_relevant_opportunities": 0,
            "opportunity_ids": [],
            "first_seen": _utc(),
            "last_seen": _utc(),
            "recommended_action": REGISTER_BEFORE_BID,
        }
    else:
        row = dict(row)

    if is_relevant_product:
        row["relevant_opportunities_seen"] = int(row.get("relevant_opportunities_seen") or 0) + 1
    if is_live and is_relevant_product:
        row["currently_live_relevant_opportunities"] = int(
            row.get("currently_live_relevant_opportunities") or 0
        ) + 1
    if opportunity_id:
        ids = list(row.get("opportunity_ids") or [])
        oid = str(opportunity_id).strip()
        if oid and oid not in ids:
            ids.append(oid)
        row["opportunity_ids"] = ids
    if buyer_name:
        row["buyer_name"] = buyer_name
    if jurisdiction:
        row["jurisdiction"] = jurisdiction
    if registration_url:
        row["registration_url"] = registration_url
    row["registration_type"] = registration_type or row.get("registration_type") or EASY_REGISTRATION
    row["last_seen"] = _utc()
    row.setdefault("first_seen", _utc())
    row.setdefault("opportunity_ids", list(row.get("opportunity_ids") or []))

    seen = int(row.get("relevant_opportunities_seen") or 0)
    status = str(row.get("registration_status") or STATUS_NOT_REGISTERED).upper()
    if status == STATUS_REGISTERED:
        row["recommended_action"] = ACTION_NONE
    elif seen >= 3 and row.get("registration_type") == EASY_REGISTRATION:
        row["recommended_action"] = REGISTER_NOW_RECURRING_BUYER
    elif is_live:
        row["recommended_action"] = REGISTER_BEFORE_BID
    else:
        row["recommended_action"] = REGISTER_NOW

    # Credentials override
    try:
        from m3_source_credentials import get_credential

        cred = get_credential(sid)
        if isinstance(cred, dict) and str(cred.get("account_status") or "").upper() in {
            "ACTIVE",
            "REGISTERED",
            "VERIFIED",
        }:
            row["registration_status"] = STATUS_REGISTERED
            row["recommended_action"] = ACTION_NONE
    except Exception:
        pass

    portals[sid] = row
    save_tracker(tracker, path)
    return row


def classify_registration_gate(
    *,
    vendor_registration_required: bool,
    lead_time_days: float | None,
    runway_days: float | None,
    source_access_level: str | None = None,
    restrictive: bool = False,
) -> dict[str, Any]:
    """Classify ordinary vs restrictive registration for access decisions."""
    if restrictive:
        return {
            "registration_gate_type": RESTRICTIVE_ELIGIBILITY,
            "registration_action": BLOCKED,
            "registration_can_be_fixed": False,
            "is_easy_registration": False,
        }
    if not vendor_registration_required:
        return {
            "registration_gate_type": NONE,
            "registration_action": ACTION_NONE,
            "registration_can_be_fixed": True,
            "is_easy_registration": True,
        }

    level = str(source_access_level or "").upper()
    easy_levels = {
        "PUBLIC",
        "FREE_REGISTRATION",
        "PUBLIC_LISTING_FREE_REG_TO_BID",
        "ACCESS_FREE_REG",
        "FREE_REG",
    }
    is_easy = level in easy_levels or level == "" or level == "UNKNOWN"

    if lead_time_days is not None and runway_days is not None:
        effective = float(runway_days) - float(lead_time_days)
        if effective < 3:
            return {
                "registration_gate_type": LONG_LEAD_REGISTRATION,
                "registration_action": BLOCKED,
                "registration_can_be_fixed": False,
                "is_easy_registration": False,
                "registration_deadline_risk": True,
                "effective_runway_days": effective,
            }
        if effective < 5:
            return {
                "registration_gate_type": REGISTRATION_WITH_DOCS if not is_easy else EASY_REGISTRATION,
                "registration_action": VERIFY_REGISTRATION_TIMING,
                "registration_can_be_fixed": True,
                "is_easy_registration": is_easy,
                "registration_deadline_risk": True,
                "effective_runway_days": effective,
            }

    # Unknown lead time: ordinary signup remains easy — does not block can_compete
    return {
        "registration_gate_type": EASY_REGISTRATION if is_easy else REGISTRATION_WITH_DOCS,
        "registration_action": REGISTER_BEFORE_BID,
        "registration_can_be_fixed": True,
        "is_easy_registration": is_easy,
        "registration_deadline_risk": False,
    }
