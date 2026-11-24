"""SAM.gov API budget guard — fail-closed, shared by every SAM API code path.

M3 has no SAM API client today. Any client added later must call ``reserve_sam_call`` before
transmitting and ``commit_sam_call`` afterwards. Limits: at most 10 calls per calendar day, and
never more than the owner-approved allocation (absent means zero), and never while
``sam_live_enabled`` is false.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

SAM_HARD_DAILY_CEILING = 10
_lock = threading.RLock()


class SamBudgetError(RuntimeError):
    pass


def _dir() -> Path:
    p = get_settings().data_root() / "sam_ledger"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(name: str) -> dict[str, Any] | None:
    p = _dir() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(state: dict[str, Any]) -> None:
    tmp = _dir() / "budget_state.tmp"
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_dir() / "budget_state.json")


def sam_authorization() -> dict[str, Any] | None:
    a = _read("owner_authorization.json")
    return a if a and a.get("authorized") else None


def sam_status() -> dict[str, Any]:
    auth = sam_authorization()
    state = _read("budget_state.json") or {}
    used = int(state.get("day_used") or 0) if state.get("day") == str(date.today()) else 0
    allocation = int((auth or {}).get("approved_allocation_calls") or 0)
    return {
        "sam_live_enabled": bool(get_settings().sam_live_enabled),
        "authorized": bool(auth),
        "approved_allocation_calls": allocation,
        "day_used": used,
        "daily_ceiling": SAM_HARD_DAILY_CEILING,
        "locked": not get_settings().sam_live_enabled or not auth or allocation <= 0,
    }


def reserve_sam_call(job_id: str) -> dict[str, Any]:
    st = sam_status()
    if st["locked"]:
        raise SamBudgetError("sam_locked: disabled or no approved allocation")
    with _lock:
        state = _read("budget_state.json") or {}
        if state.get("day") != str(date.today()):
            state = {**state, "day": str(date.today()), "day_used": 0, "day_reserved": 0}
        auth_key = str((sam_authorization() or {}).get("written_at"))
        if state.get("allocation_key") != auth_key:
            state.update(allocation_key=auth_key, allocation_used=0)
        committed = int(state.get("day_used") or 0) + int(state.get("day_reserved") or 0)
        if committed + 1 > SAM_HARD_DAILY_CEILING:
            raise SamBudgetError(f"sam_daily_ceiling:{SAM_HARD_DAILY_CEILING}")
        if int(state.get("allocation_used") or 0) + int(state.get("day_reserved") or 0) + 1 > st["approved_allocation_calls"]:
            raise SamBudgetError(f"sam_approved_allocation:{st['approved_allocation_calls']}")
        state["day_reserved"] = int(state.get("day_reserved") or 0) + 1
        _write_state(state)
    return {"reservation_id": str(uuid.uuid4()), "job_id": job_id, "at": datetime.now(timezone.utc).isoformat()}


def commit_sam_call(reservation: dict[str, Any], *, sent: bool) -> None:
    """``sent`` covers uncertain outcomes too: a transmitted request is always counted."""
    with _lock:
        state = _read("budget_state.json") or {}
        state["day_reserved"] = max(0, int(state.get("day_reserved") or 0) - 1)
        if sent:
            state["day_used"] = int(state.get("day_used") or 0) + 1
            state["allocation_used"] = int(state.get("allocation_used") or 0) + 1
        _write_state(state)
