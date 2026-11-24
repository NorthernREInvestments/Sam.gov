"""Automatic NextBid acquisition scheduling (Build 002I).

Emergency stop: scheduler will not start or run while NEXTBID_EMERGENCY_STOP /
nextbid_enabled=false. Await owner authorization before resuming.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.nextbid_killswitch import emergency_stop_status

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
    if not (settings.nextbid_api_key or "").strip():
        return {"ok": False, "skipped": "missing_api_key", "requires_secure_configuration": True}

    interval = max(30, int(settings.nextbid_schedule_minutes)) * 60
    now = time.time()
    if not force and _last_run_at and (now - _last_run_at) < interval:
        return {"ok": True, "skipped": "not_due", "seconds_remaining": int(interval - (now - _last_run_at))}

    from m3.research.nextbid_acquire import run_nextbid_acquisition

    result = run_nextbid_acquisition(
        max_discovery_calls=40,
        max_detail=120,
        detail_workers=4,
        checkpoint_name="nextbid_acquire_v1",
    )
    _last_run_at = time.time()
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
                try:
                    run_scheduled_acquisition(force=False)
                except Exception as exc:
                    _write_status(
                        {
                            "started": True,
                            "paused": _stop_event.is_set(),
                            "last_error": type(exc).__name__,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                # Re-check stop frequently rather than sleeping full interval blindly
                for _ in range(max(60, int(settings.nextbid_schedule_minutes) * 60)):
                    if _stop_event.is_set() or not emergency_stop_status().get("metered_calls_allowed"):
                        break
                    time.sleep(1)

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
