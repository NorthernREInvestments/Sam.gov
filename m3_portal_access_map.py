"""Portal → account → membership mapping (no secrets).

Persists under M3_DATA_ROOT so Railway volume survives restarts/deployments.
Used so M3 reuses existing portal logins and completed purchasing-group /
membership registrations instead of creating duplicate accounts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.m3_portal_access_map")

BUILD = "20261014-m3-portal-access-map-v1"
MAP_REL = "m3_auth/portal_access_map_v1.json"
_lock = threading.RLock()

# Canonical portal keys used across M3
PORTAL_BIDNET = "bidnet"
PORTAL_OPENGOV = "opengov"
PORTAL_EUNA = "euna"
PORTAL_SAM = "sam"


def _utc() -> str:
    return now_utc().isoformat()


def map_path() -> Path:
    return Path(data_path(MAP_REL))


def empty_map() -> dict[str, Any]:
    return {
        "kind": "M3PortalAccessMap",
        "build": BUILD,
        "updated_at": _utc(),
        "portals": {},
        "rules": [
            "REUSE_EXISTING_ACCOUNTS",
            "NO_DUPLICATE_REGISTRATIONS",
            "NO_SECRETS_IN_THIS_FILE",
            "SESSIONS_UNDER_M3_DATA_ROOT",
            "OWNER_APPROVAL_TO_CHANGE_CREDENTIALS",
        ],
    }


def load_map() -> dict[str, Any]:
    path = map_path()
    if not path.exists():
        return empty_map()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return empty_map()
        obj.setdefault("portals", {})
        obj.setdefault("build", BUILD)
        return obj
    except Exception:
        return empty_map()


def save_map(state: dict[str, Any]) -> Path:
    path = map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = deepcopy(state)
    state["kind"] = "M3PortalAccessMap"
    state["build"] = BUILD
    state["updated_at"] = _utc()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    return path


def ensure_portal(
    portal: str,
    *,
    account_ref: str | None = None,
    username_hint: str | None = None,
    session_relpath: str | None = None,
    login_url: str | None = None,
) -> dict[str, Any]:
    """Create/update portal account binding. Never stores passwords."""
    key = str(portal or "").strip().lower().replace(" ", "_")
    if not key:
        raise ValueError("portal required")
    with _lock:
        state = load_map()
        cur = dict((state.get("portals") or {}).get(key) or {})
        cur.setdefault("portal", key)
        cur.setdefault("memberships", {})
        cur.setdefault("first_seen_at", _utc())
        if account_ref:
            # Do not silently switch account_ref if one already exists
            existing_ref = cur.get("account_ref")
            if existing_ref and existing_ref != account_ref:
                cur["alternate_account_ref_attempted"] = account_ref
                cur["account_switch_blocked"] = True
            else:
                cur["account_ref"] = account_ref
        if username_hint and not cur.get("username_hint"):
            # Store only first-seen hint; don't overwrite
            cur["username_hint"] = str(username_hint)[:120]
        if session_relpath:
            cur["session_relpath"] = session_relpath
        if login_url and not cur.get("login_url"):
            cur["login_url"] = str(login_url)[:220]
        cur["updated_at"] = _utc()
        state.setdefault("portals", {})[key] = cur
        save_map(state)
        return cur


def bind_membership(
    portal: str,
    membership_id: str,
    *,
    kind: str = "purchasing_group",
    status: str,
    name: str | None = None,
    opportunity_id: str | None = None,
) -> dict[str, Any]:
    """Record completed/pending portal membership (e.g. BidNet purchasing group)."""
    key = str(portal or "").strip().lower().replace(" ", "_")
    mid = str(membership_id or "").strip()
    if not key or not mid:
        raise ValueError("portal and membership_id required")
    with _lock:
        state = load_map()
        portal_rec = dict((state.get("portals") or {}).get(key) or {"portal": key, "memberships": {}})
        portal_rec.setdefault("memberships", {})
        mem = dict(portal_rec["memberships"].get(mid) or {})
        mem["membership_id"] = mid
        mem["kind"] = kind
        mem["status"] = status
        if name:
            mem["name"] = name
        mem["updated_at"] = _utc()
        if status == "REGISTERED" and not mem.get("registered_at"):
            mem["registered_at"] = _utc()
        if opportunity_id:
            blocked = list(mem.get("opportunity_ids") or [])
            if opportunity_id not in blocked:
                blocked.append(opportunity_id)
            mem["opportunity_ids"] = blocked[-200:]
        portal_rec["memberships"][mid] = mem
        portal_rec["updated_at"] = _utc()
        state.setdefault("portals", {})[key] = portal_rec
        save_map(state)
        return mem


def get_membership(portal: str, membership_id: str) -> dict[str, Any] | None:
    key = str(portal or "").strip().lower().replace(" ", "_")
    mid = str(membership_id or "").strip()
    portal_rec = (load_map().get("portals") or {}).get(key) or {}
    return (portal_rec.get("memberships") or {}).get(mid)


def is_membership_registered(portal: str, membership_id: str) -> bool:
    mem = get_membership(portal, membership_id) or {}
    return str(mem.get("status") or "").upper() == "REGISTERED"


def ui_summary() -> dict[str, Any]:
    state = load_map()
    portals_out = []
    for key, rec in (state.get("portals") or {}).items():
        mems = list((rec.get("memberships") or {}).values())
        portals_out.append(
            {
                "portal": key,
                "account_ref": rec.get("account_ref"),
                "username_hint": rec.get("username_hint"),
                "session_relpath": rec.get("session_relpath"),
                "login_url": rec.get("login_url"),
                "membership_count": len(mems),
                "registered_memberships": sum(
                    1 for m in mems if str(m.get("status") or "").upper() == "REGISTERED"
                ),
                "account_switch_blocked": bool(rec.get("account_switch_blocked")),
            }
        )
    return {
        "kind": "M3PortalAccessMapUI",
        "build": BUILD,
        "updated_at": state.get("updated_at"),
        "portals": portals_out,
        "path": str(map_path()),
        "operator_note": (
            "Access map binds portals to existing accounts and memberships. "
            "Secrets live in encrypted credential store / env — never here."
        ),
    }


def seed_bidnet_defaults(*, username_hint: str | None = None) -> dict[str, Any]:
    """Ensure BidNet portal row points at durable session path + credential ref."""
    return ensure_portal(
        PORTAL_BIDNET,
        account_ref=PORTAL_BIDNET,
        username_hint=username_hint,
        session_relpath="bidnet_auth/storage_state.json",
        login_url="https://www.bidnetdirect.com/public/authentication/login",
    )
