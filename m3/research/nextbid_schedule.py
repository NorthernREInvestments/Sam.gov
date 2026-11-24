"""Automatic NextBid acquisition scheduling (Build 002I).

Emergency stop: scheduler will not start or run while NEXTBID_EMERGENCY_STOP /
nextbid_enabled=false. Await owner authorization before resuming.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.nextbid_killswitch import emergency_stop_status

SLOT_DISCOVERY_CALLS = 10
SLOT_DETAIL_CALLS = 40

_scheduler_lock = threading.Lock()
_scheduler_started = False
_last_run_at: float | None = None
_last_result: dict[str, Any] | None = None
_stop_event = threading.Event()


def _status_path() -> Path:
    p = get_settings().data_root() / "nextbid_checkpoints" / "scheduler_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def scheduler_status() -> dict[str, Any]:
    path = _status_path()
    base: dict[str, Any] = {
        "started": _scheduler_started,
        "last_run_at": None,
        "last_ok": None,
        "paused": _stop_event.is_set(),
        "killswitch": emergency_stop_status(),
    }
    if path.exists():
        try:
            file_st = json.loads(path.read_text(encoding="utf-8"))
            base.update(file_st)
            base["killswitch"] = emergency_stop_status()
            base["paused"] = _stop_event.is_set() or not emergency_stop_status().get(
                "metered_calls_allowed"
            )
        except Exception:
            pass
    return base


def _write_status(payload: dict[str, Any]) -> None:
    _status_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def pause_scheduler(*, reason: str = "owner_emergency_stop") -> dict[str, Any]:
    """Stop future scheduled runs immediately (in-process + durable flag)."""
    _stop_event.set()
    payload = {
        "started": _scheduler_started,
        "paused": True,
        "pause_reason": reason,
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "metered_acquisition_forbidden": True,
        "resume_requires_owner_authorization": True,
        "killswitch": emergency_stop_status(),
    }
    _write_status(payload)
    return payload


def _slots() -> list[tuple[int, int]]:
    out = []
    for part in str(get_settings().discovery_schedule_slots or "").split(","):
        hh, _, mm = part.strip().partition(":")
        if hh.isdigit() and (mm or "0").isdigit() and int(hh) < 24 and int(mm or 0) < 60:
            out.append((int(hh), int(mm or 0)))
    return sorted(set(out))


def next_discovery_slot(now: datetime | None = None) -> datetime | None:
    """Next configured daily slot (UTC) after ``now``; None when no valid slot is configured."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(get_settings().discovery_schedule_timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    for day in range(2):
        base = (local + timedelta(days=day)).date()
        for hh, mm in _slots():
            cand = datetime(base.year, base.month, base.day, hh, mm, tzinfo=tz)
            if cand > local:
                return cand.astimezone(timezone.utc)
    return None


def schedule_plan(now: datetime | None = None) -> dict[str, Any]:
    """What would run and whether every gate currently permits it (it does not, until the owner unlocks)."""
    s = get_settings()
    kill = emergency_stop_status()
    gates = {
        "discovery_schedule_enabled": bool(s.discovery_schedule_enabled),
        "nextbid_enabled": bool(s.nextbid_enabled),
        "metered_calls_allowed": bool(kill.get("metered_calls_allowed")),
        "scheduler_not_paused": not _stop_event.is_set(),
    }
    nxt = next_discovery_slot(now)
    return {
        "slots_local": [f"{h:02d}:{m:02d}" for h, m in _slots()],
        "timezone": s.discovery_schedule_timezone,
        "next_slot_utc": nxt.isoformat() if nxt else None,
        "per_run": {"max_discovery_calls": SLOT_DISCOVERY_CALLS, "max_detail": SLOT_DETAIL_CALLS,
                    "job_ceiling": 50, "daily_ceiling": 100, "then": "staged_inventory_rebuild_offline"},
        "gates": gates,
        "would_run": all(gates.values()),
    }


def run_scheduled_acquisition(*, force: bool = False) -> dict[str, Any]:
    """Run a budgeted NextBid acquisition if due — blocked under emergency stop."""
    global _last_run_at, _last_result
    settings = get_settings()
    kill = emergency_stop_status()
    if _stop_event.is_set() or not kill.get("metered_calls_allowed"):
        return {
            "ok": False,
            "skipped": "emergency_stop",
            "killswitch": kill,
            "resume_requires_owner_authorization": True,
        }
    if not settings.nextbid_enabled:
        return {"ok": False, "skipped": "nextbid_disabled"}
    if not settings.discovery_schedule_enabled:
        return {"ok": False, "skipped": "daily_schedule_disabled"}
    if not (settings.nextbid_api_key or "").strip():
        return {"ok": False, "skipped": "missing_api_key", "requires_secure_configuration": True}

    interval = max(30, int(settings.nextbid_schedule_minutes)) * 60
    now = time.time()
    if not force and _last_run_at and (now - _last_run_at) < interval:
        return {"ok": True, "skipped": "not_due", "seconds_remaining": int(interval - (now - _last_run_at))}

    from m3.research.nextbid_acquire import run_nextbid_acquisition

    # Discovery + detail together stay within the controller's 50-call job ceiling;
    # two daily slots stay within the 100-call daily ceiling
    result = run_nextbid_acquisition(
        max_discovery_calls=SLOT_DISCOVERY_CALLS,
        max_detail=SLOT_DETAIL_CALLS,
        detail_workers=4,
        checkpoint_name="nextbid_acquire_v1",
    )
    _last_run_at = time.time()
    staged: dict[str, Any] | None = None
    if result.get("ok"):
        from m3.research.staged_inventory import run_staged_inventory

        try:
            m = run_staged_inventory(resume=False, require_locked=False)
            staged = {"queue_counts": m.get("queue_counts"), "all_records_dispositioned": m.get("all_records_dispositioned")}
        except Exception as exc:
            staged = {"error": type(exc).__name__}
    result["staged_inventory"] = staged
    _last_result = {
        "ok": result.get("ok"),
        "finished_at": result.get("finished_at"),
        "fits": (result.get("qualification") or {}).get("strict_m3_procurement_fits"),
        "discovered": (result.get("discovery") or {}).get("discovered"),
        "usage_after": result.get("usage_after"),
    }
    _write_status(
        {
            "started": True,
            "paused": False,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_ok": bool(result.get("ok")),
            "last_result": _last_result,
            "interval_minutes": settings.nextbid_schedule_minutes,
        }
    )
    return result


def start_nextbid_scheduler() -> bool:
    """Start daemon thread. Will NOT start under emergency stop / disabled flag."""
    global _scheduler_started
    settings = get_settings()
    with _scheduler_lock:
        kill = emergency_stop_status()
        if not kill.get("metered_calls_allowed"):
            pause_scheduler(reason="blocked_at_startup_emergency_stop")
            return False
        if _scheduler_started:
            return False
        if not settings.nextbid_enabled:
            pause_scheduler(reason="nextbid_disabled")
            return False
        if not settings.discovery_schedule_enabled:
            pause_scheduler(reason="daily_schedule_disabled")
            return False
        if not (settings.nextbid_api_key or "").strip():
            _write_status(
                {
                    "started": False,
                    "paused": True,
                    "error": "NEXTBID_API_KEY missing — secure configuration required",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return False

        _stop_event.clear()

        def _loop() -> None:
            time.sleep(15)
            while not _stop_event.is_set():
                if not emergency_stop_status().get("metered_calls_allowed"):
                    pause_scheduler(reason="killswitch_engaged_mid_loop")
                    break
                nxt = next_discovery_slot()
                if nxt is None:
                    pause_scheduler(reason="no_valid_daily_slot")
                    break
                while not _stop_event.is_set() and datetime.now(timezone.utc) < nxt:
                    if not emergency_stop_status().get("metered_calls_allowed"):
                        break
                    time.sleep(min(60.0, max(1.0, (nxt - datetime.now(timezone.utc)).total_seconds())))
                if _stop_event.is_set() or not emergency_stop_status().get("metered_calls_allowed"):
                    continue
                try:
                    run_scheduled_acquisition(force=True)
                except Exception as exc:
                    _write_status(
                        {
                            "started": True,
                            "paused": _stop_event.is_set(),
                            "last_error": type(exc).__name__,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
        th = threading.Thread(target=_loop, name="nextbid-scheduler", daemon=True)
        th.start()
        _scheduler_started = True
        _write_status(
            {
                "started": True,
                "paused": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "interval_minutes": settings.nextbid_schedule_minutes,
                "key_configured": True,
            }
        )
        return True
