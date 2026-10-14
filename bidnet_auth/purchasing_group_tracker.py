"""Persistent BidNet purchasing-group registration status tracker.

Compact records only — status, confirmation, opportunity unlock counts.
Stored under M3 data root (survives Railway volume when M3_DATA_ROOT=/data).
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

BUILD = "20261013-m3-bidnet-pg-registration-v1"
TRACKER_REL = "bidnet_auth/purchasing_group_access_tracker_v1.json"

STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_REQUIRED = "NOT_REQUIRED"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_REGISTERED = "REGISTERED"
STATUS_OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
STATUS_PAID_REQUIRED = "PAID_REQUIRED"
STATUS_ERROR = "ERROR"
STATUS_PREPARED = "PREPARED"

_lock = threading.RLock()  # re-entrant: upsert_group → save_tracker


def _utc() -> str:
    return now_utc().isoformat()


def tracker_path() -> Path:
    return Path(data_path(TRACKER_REL))


def empty_tracker() -> dict[str, Any]:
    return {
        "kind": "BidNetPurchasingGroupAccessTracker",
        "build": BUILD,
        "updated_at": _utc(),
        "groups": {},
        "pending_owner_actions": [],
        "metrics": {
            "groups_identified": 0,
            "relevant": 0,
            "registered": 0,
            "incomplete": 0,
            "owner_approval_required": 0,
            "paid_required": 0,
            "registrations_completed": 0,
            "failures": 0,
            "opportunities_potentially_unlocked": 0,
            "solicitations_unlocked": 0,
        },
    }


def load_tracker() -> dict[str, Any]:
    path = tracker_path()
    if not path.exists():
        return empty_tracker()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return empty_tracker()
        obj.setdefault("groups", {})
        obj.setdefault("pending_owner_actions", [])
        obj.setdefault("metrics", {})
        return obj
    except Exception:
        return empty_tracker()


def save_tracker(tracker: dict[str, Any]) -> Path:
    path = tracker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tracker = deepcopy(tracker)
    tracker["updated_at"] = _utc()
    tracker["build"] = BUILD
    # recompute metrics
    groups = tracker.get("groups") or {}
    metrics = {
        "groups_identified": len(groups),
        "relevant": sum(1 for g in groups.values() if g.get("relevant")),
        "registered": sum(1 for g in groups.values() if g.get("status") == STATUS_REGISTERED),
        "incomplete": sum(1 for g in groups.values() if g.get("status") == STATUS_INCOMPLETE),
        "owner_approval_required": sum(
            1 for g in groups.values() if g.get("status") == STATUS_OWNER_APPROVAL_REQUIRED
        ),
        "paid_required": sum(1 for g in groups.values() if g.get("status") == STATUS_PAID_REQUIRED),
        "registrations_completed": sum(1 for g in groups.values() if g.get("registration_completed_at")),
        "failures": sum(1 for g in groups.values() if g.get("status") == STATUS_ERROR),
        "opportunities_potentially_unlocked": sum(
            int(g.get("live_eligible_opportunity_count") or 0) for g in groups.values() if g.get("relevant")
        ),
        "solicitations_unlocked": sum(int(g.get("solicitations_unlocked") or 0) for g in groups.values()),
        "prepared": sum(1 for g in groups.values() if g.get("status") == STATUS_PREPARED),
    }
    tracker["metrics"] = metrics
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(tracker, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    return path


def upsert_group(pg_id: str, **fields: Any) -> dict[str, Any]:
    pg_id = str(pg_id or "").strip()
    if not pg_id:
        raise ValueError("pg_id required")
    with _lock:
        tracker = load_tracker()
        cur = dict((tracker.get("groups") or {}).get(pg_id) or {})
        cur.setdefault("purchasing_group_id", pg_id)
        cur.setdefault("first_seen_at", _utc())
        cur.update({k: v for k, v in fields.items() if v is not None})
        cur["updated_at"] = _utc()
        tracker.setdefault("groups", {})[pg_id] = cur
        # pending owner actions index
        pending = [
            a
            for a in (tracker.get("pending_owner_actions") or [])
            if str(a.get("purchasing_group_id")) != pg_id
        ]
        if cur.get("status") == STATUS_OWNER_APPROVAL_REQUIRED:
            pending.append(
                {
                    "purchasing_group_id": pg_id,
                    "name": cur.get("name"),
                    "reason": cur.get("owner_blocker_reason"),
                    "missing_fields": cur.get("missing_fields") or [],
                    "at": _utc(),
                }
            )
        tracker["pending_owner_actions"] = pending
        save_tracker(tracker)
        return cur


def get_group(pg_id: str) -> dict[str, Any] | None:
    return (load_tracker().get("groups") or {}).get(str(pg_id))


def is_registered(pg_id: str) -> bool:
    g = get_group(pg_id) or {}
    return g.get("status") == STATUS_REGISTERED


def ui_summary() -> dict[str, Any]:
    t = load_tracker()
    groups = list((t.get("groups") or {}).values())
    return {
        "kind": "PurchasingGroupAccessUI",
        "build": BUILD,
        "updated_at": t.get("updated_at"),
        "metrics": t.get("metrics") or {},
        "pending_owner_actions": t.get("pending_owner_actions") or [],
        "groups": sorted(
            groups,
            key=lambda g: (-int(g.get("live_eligible_opportunity_count") or 0), str(g.get("name") or "")),
        ),
        "operator_note": (
            "Complete owner actions once; M3 remembers registration status across jobs. "
            "No paid registrations without explicit approval."
        ),
    }
