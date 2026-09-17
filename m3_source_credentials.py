"""Simple persistent portal credential store — functionality first, harden later."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_credentials")

SETTINGS_KEY = "m3_portal_credentials_v1"


def _utc() -> str:
    return now_utc().isoformat()


def _load() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    return data
        finally:
            db.close()
    except Exception:
        log.debug("credential load failed", exc_info=True)
    return {"kind": "M3PortalCredentials", "portals": {}, "updated_at": None}


def _save(state: dict[str, Any]) -> None:
    state = deepcopy(state)
    state["kind"] = "M3PortalCredentials"
    state["updated_at"] = _utc()
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            payload = json.dumps(state, default=str)
            if row:
                row.value = payload
            else:
                db.add(AppSetting(key=SETTINGS_KEY, value=payload))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("credential save failed")


def list_credentials(*, include_secrets: bool = False) -> dict[str, Any]:
    state = _load()
    portals = {}
    for key, rec in (state.get("portals") or {}).items():
        r = dict(rec)
        if not include_secrets:
            if r.get("password"):
                r["password"] = "********" if r.get("password") else ""
                r["password_set"] = True
            else:
                r["password_set"] = False
        portals[key] = r
    return {"kind": "M3PortalCredentials", "portals": portals, "updated_at": state.get("updated_at")}


def upsert_credential(portal: str, body: dict[str, Any]) -> dict[str, Any]:
    portal_key = str(portal or body.get("portal") or "").strip().lower().replace(" ", "_")
    if not portal_key:
        return {"ok": False, "error": "portal required"}
    state = _load()
    portals = dict(state.get("portals") or {})
    existing = dict(portals.get(portal_key) or {})
    password = body.get("password")
    if password == "********":
        password = existing.get("password")
    rec = {
        "portal": portal_key,
        "source_id": body.get("source_id") or existing.get("source_id") or portal_key,
        "login_url": body.get("login_url") or existing.get("login_url"),
        "username": body.get("username") if body.get("username") is not None else existing.get("username"),
        "password": password if password is not None else existing.get("password"),
        "account_notes": body.get("account_notes") if body.get("account_notes") is not None else existing.get("account_notes"),
        "account_status": body.get("account_status") or existing.get("account_status") or "CONFIGURED",
        "last_successful_login": existing.get("last_successful_login"),
        "last_failure": existing.get("last_failure"),
        "mfa_or_manual_login_required": bool(
            body.get("mfa_or_manual_login_required")
            if body.get("mfa_or_manual_login_required") is not None
            else existing.get("mfa_or_manual_login_required")
        ),
        "updated_at": _utc(),
    }
    portals[portal_key] = rec
    state["portals"] = portals
    _save(state)
    return {"ok": True, "credential": list_credentials()["portals"].get(portal_key)}


def get_credential(portal: str, *, include_secrets: bool = False) -> dict[str, Any] | None:
    key = str(portal or "").strip().lower().replace(" ", "_")
    state = _load()
    rec = (state.get("portals") or {}).get(key)
    if not rec:
        return None
    if include_secrets:
        return dict(rec)
    out = dict(rec)
    out["password_set"] = bool(out.pop("password", None))
    return out


def mark_login_result(portal: str, *, success: bool, reason: str | None = None) -> None:
    state = _load()
    key = str(portal or "").strip().lower().replace(" ", "_")
    rec = dict((state.get("portals") or {}).get(key) or {"portal": key})
    if success:
        rec["last_successful_login"] = _utc()
        rec["account_status"] = "AUTHENTICATED"
        rec["last_failure"] = None
    else:
        rec["last_failure"] = {"at": _utc(), "reason": reason or "auth_failed"}
        rec["account_status"] = "AUTH_FAILED"
    portals = dict(state.get("portals") or {})
    portals[key] = rec
    state["portals"] = portals
    _save(state)
