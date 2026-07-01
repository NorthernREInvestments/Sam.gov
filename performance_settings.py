"""WAWF password and IPP registration settings."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from database import SessionLocal
from models import AppSetting
from performance_constants import WAWF_PASSWORD_CYCLE_DAYS, WAWF_PASSWORD_WARN_DAYS

WAWF_PASSWORD_DATE_KEY = "wawf_last_password_change"
IPP_REGISTERED_KEY = "ipp_registered"


def _get_json(key: str) -> Any:
    session = SessionLocal()
    try:
        row = session.get(AppSetting, key)
        if not row:
            return None
        return json.loads(row.value)
    except Exception:
        return row.value if row else None
    finally:
        session.close()


def _set_json(key: str, value: Any) -> None:
    session = SessionLocal()
    try:
        row = session.get(AppSetting, key)
        payload = json.dumps(value)
        if row:
            row.value = payload
        else:
            session.add(AppSetting(key=key, value=payload))
        session.commit()
    finally:
        session.close()


def get_performance_settings() -> dict[str, Any]:
    wawf_raw = _get_json(WAWF_PASSWORD_DATE_KEY)
    wawf_date = wawf_raw if isinstance(wawf_raw, str) else None
    ipp = _get_json(IPP_REGISTERED_KEY)
    status = wawf_password_status(wawf_date)
    return {
        "wawf_last_password_change": wawf_date,
        "wawf_next_due": status.get("next_due"),
        "wawf_status": status,
        "ipp_registered": bool(ipp),
    }


def save_performance_settings(*, wawf_last_password_change: str | None = None, ipp_registered: bool | None = None) -> dict[str, Any]:
    if wawf_last_password_change is not None:
        _set_json(WAWF_PASSWORD_DATE_KEY, wawf_last_password_change)
    if ipp_registered is not None:
        _set_json(IPP_REGISTERED_KEY, ipp_registered)
    return get_performance_settings()


def wawf_password_status(last_change: str | None = None) -> dict[str, Any]:
    if last_change is None:
        raw = _get_json(WAWF_PASSWORD_DATE_KEY)
        last_change = raw if isinstance(raw, str) else None
    if not last_change:
        return {"level": "neutral", "days_remaining": None, "message": None, "next_due": None}
    try:
        changed = date.fromisoformat(last_change)
    except ValueError:
        return {"level": "neutral", "days_remaining": None, "message": None, "next_due": None}
    next_due = changed + timedelta(days=WAWF_PASSWORD_CYCLE_DAYS)
    days_left = (next_due - date.today()).days
    if days_left < 0:
        return {
            "level": "red",
            "days_remaining": days_left,
            "next_due": next_due.isoformat(),
            "message": "WAWF password has expired — update immediately at piee.eb.mil to avoid invoicing delays.",
        }
    if days_left <= WAWF_PASSWORD_WARN_DAYS:
        return {
            "level": "yellow",
            "days_remaining": days_left,
            "next_due": next_due.isoformat(),
            "message": f"WAWF password expires in {days_left} days — update your password at piee.eb.mil now.",
        }
    return {
        "level": "green",
        "days_remaining": days_left,
        "next_due": next_due.isoformat(),
        "message": None,
    }


def ipp_reminder_active() -> dict[str, Any] | None:
    ipp = _get_json(IPP_REGISTERED_KEY)
    if ipp:
        return None
    return {
        "message": (
            "Have you registered at ipp.gov for civilian agency invoicing? Forest Service, Fish and Wildlife, "
            "and Army Corps contracts pay through IPP not WAWF. Register at ipp.gov using your SAM.gov business "
            "information and CAGE code. Mark complete when done."
        ),
        "link": "https://www.ipp.gov",
    }
