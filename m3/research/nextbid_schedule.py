"""Automatic research scheduling (NextBid + free-source slots).

Owner schedule (local SCHEDULER_TIMEZONE, default America/Denver):
- Free portals: 08:00 and 15:00 — full board + change-watch fingerprint
- Paid (NextBid): 08:00 — budgeted API calls only

NextBid: emergency stop / NEXTBID_ENABLED=false blocks metered acquisition.
Free slots: independent of NextBid lock (public portals).
SCHEDULER_ENABLED=false keeps daemons from starting.
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
from m3.research.schedule_policy import (
    due_free_slots,
    local_now,
    paid_due,
    paid_refresh_slot,
    schedule_summary,
    schedule_timezone,
)

_scheduler_lock = threading.Lock()
_scheduler_started = False
_last_run_at: float | None = None
_last_result: dict[str, Any] | None = None
_last_paid_run_day: str | None = None
_stop_event = threading.Event()

_free_scheduler_lock = threading.Lock()
_free_scheduler_started = False
_free_stop_event = threading.Event()
_free_ran_slots: set[str] = set()
_free_last_result: dict[str, Any] | None = None


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
        "schedule": schedule_summary(),
        "mode": "paid_daily_at_local_hour",
    }
    if path.exists():
        try:
            file_st = json.loads(path.read_text(encoding="utf-8"))
            base.update(file_st)
            base["killswitch"] = emergency_stop_status()
            base["paused"] = _stop_event.is_set() or not emergency_stop_status().get(
                "metered_calls_allowed"
            )
            base["schedule"] = schedule_summary()
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
        "schedule": schedule_summary(),
    }
    _write_status(payload)
    return payload


def run_scheduled_acquisition(*, force: bool = False) -> dict[str, Any]:
    """Run a budgeted NextBid acquisition once per local day at the paid slot (default 08:00)."""
    global _last_run_at, _last_result, _last_paid_run_day
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

    now_local = local_now()
    due_info = paid_due(now_local=now_local, last_run_day=_last_paid_run_day)
    if not force:
        if not due_info["due"]:
            return {
                "ok": True,
                "skipped": "already_ran_today" if due_info["already_ran_today"] else "not_due_yet",
                "schedule": due_info,
                "runs_at": f"{due_info['hour']:02d}:{due_info['minute']:02d} {schedule_timezone()}",
            }

    # Cap discovery/detail by configured daily + job call limits
    daily_cap = max(1, int(settings.nextbid_daily_call_limit))
    job_cap = max(1, int(settings.nextbid_job_call_limit))
    max_calls = min(daily_cap, job_cap)
    # Split budget: prefer discovery then detail within job cap
    max_discovery = min(int(settings.nextbid_budget_discovery), max(1, max_calls // 2))
    max_detail = min(int(settings.nextbid_budget_detail), max(0, max_calls - max_discovery))

    from m3.research.nextbid_acquire import run_nextbid_acquisition

    result = run_nextbid_acquisition(
        max_discovery_calls=max_discovery,
        max_detail=max_detail,
        detail_workers=min(4, int(settings.nextbid_max_concurrency or 4)),
        checkpoint_name="nextbid_acquire_v1",
    )
    _last_run_at = time.time()
    _last_paid_run_day = due_info["day"]
    _last_result = {
        "ok": result.get("ok"),
        "finished_at": result.get("finished_at"),
        "fits": (result.get("qualification") or {}).get("strict_m3_procurement_fits"),
        "discovered": (result.get("discovery") or {}).get("discovered"),
        "usage_after": result.get("usage_after"),
        "call_budget": {
            "daily_cap": daily_cap,
            "job_cap": job_cap,
            "max_discovery": max_discovery,
            "max_detail": max_detail,
        },
        "slot": due_info["slot"],
    }
    _write_status(
        {
            "started": True,
            "paused": False,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_ok": bool(result.get("ok")),
            "last_result": _last_result,
            "last_paid_run_day_local": _last_paid_run_day,
            "mode": "paid_daily_at_local_hour",
            "schedule": schedule_summary(),
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
                    "schedule": schedule_summary(),
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
                            "schedule": schedule_summary(),
                        }
                    )
                # Poll about once a minute for the 08:00 local window
                for _ in range(60):
                    if _stop_event.is_set() or not emergency_stop_status().get("metered_calls_allowed"):
                        break
                    time.sleep(1)

        th = threading.Thread(target=_loop, name="nextbid-scheduler", daemon=True)
        th.start()
        _scheduler_started = True
        hour, minute = paid_refresh_slot()
        _write_status(
            {
                "started": True,
                "paused": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "mode": "paid_daily_at_local_hour",
                "runs_at_local": f"{hour:02d}:{minute:02d}",
                "timezone": schedule_timezone(),
                "daily_call_limit": settings.nextbid_daily_call_limit,
                "job_call_limit": settings.nextbid_job_call_limit,
                "key_configured": True,
                "schedule": schedule_summary(),
            }
        )
        return True


def _free_status_path() -> Path:
    p = get_settings().data_root() / "free_source_keep" / "daily_scheduler_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _free_scheduler_enabled() -> bool:
    import os

    return (os.getenv("SCHEDULER_ENABLED") or "").strip().lower() in {"1", "true", "yes"}


def free_source_scheduler_status() -> dict[str, Any]:
    base: dict[str, Any] = {
        "started": _free_scheduler_started,
        "paused": _free_stop_event.is_set(),
        "ran_slots_today": sorted(_free_ran_slots),
        "last_result": _free_last_result,
        "config": {
            "enabled": _free_scheduler_enabled(),
            **schedule_summary(),
        },
        "note": (
            "Free portals: twice daily (default 08:00 + 15:00 local); "
            "no NextBid credits; independent of NextBid lock"
        ),
    }
    path = _free_status_path()
    if path.exists():
        try:
            file_st = json.loads(path.read_text(encoding="utf-8"))
            base.update(file_st)
            base["config"] = {
                "enabled": _free_scheduler_enabled(),
                **schedule_summary(),
            }
        except Exception:
            pass
    return base


def pause_free_source_scheduler(*, reason: str = "owner_pause") -> dict[str, Any]:
    _free_stop_event.set()
    payload = {
        "started": _free_scheduler_started,
        "paused": True,
        "pause_reason": reason,
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "config": {"enabled": _free_scheduler_enabled(), **schedule_summary()},
    }
    _free_status_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def run_scheduled_free_sources_daily(*, force: bool = False) -> dict[str, Any]:
    """Pull every wired free portal for each due slot (08:00 and 15:00 by default)."""
    global _free_last_result, _free_ran_slots
    now_local = local_now()
    day = now_local.date().isoformat()
    # Drop yesterday's slot keys
    _free_ran_slots = {k for k in _free_ran_slots if k.startswith(day)}

    if force:
        slots = due_free_slots(now_local=now_local, already_ran=set())
        if not slots:
            # Force the nearest configured slot label for reporting
            from m3.research.schedule_policy import free_refresh_slots, slot_key

            h, m = free_refresh_slots()[0]
            slots = [{"slot": slot_key(day, h, m), "hour": h, "minute": m, "day": day}]
    else:
        slots = due_free_slots(now_local=now_local, already_ran=_free_ran_slots)
        if not slots:
            return {
                "ok": True,
                "skipped": "no_free_slot_due",
                "day": day,
                "ran_slots_today": sorted(_free_ran_slots),
                "schedule": schedule_summary(),
            }

    from m3.research.free_source_step_pipeline import run_all_free_sources_daily

    slot_results: list[dict[str, Any]] = []
    for slot in slots:
        result = run_all_free_sources_daily(read_docs=True)
        # Fingerprint packages for same-day change orders after each free pull
        change_watch = None
        try:
            from m3.db import SessionLocal
            from m3.models import SolicitationPackage
            from m3.research.change_watch import scan_package_for_changes

            db = SessionLocal()
            try:
                pkgs = (
                    db.query(SolicitationPackage)
                    .order_by(SolicitationPackage.updated_at.desc())
                    .limit(300)
                    .all()
                )
                cw = [scan_package_for_changes(db, p, live_docs=None) for p in pkgs]
                db.commit()
                change_watch = {
                    "scanned": len(cw),
                    "changes_detected": sum(1 for r in cw if r.get("changed")),
                }
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            change_watch = {"ok": False, "error": f"{type(exc).__name__}:{exc}"[:200]}

        _free_ran_slots.add(slot["slot"])
        slot_results.append(
            {
                "slot": slot["slot"],
                "ok": True,
                "kept_total": result.get("kept_total"),
                "trashed_total": result.get("trashed_total"),
                "sources": [r.get("source") for r in (result.get("results") or [])],
                "finished_at": result.get("finished_at"),
                "report": result.get("report"),
                "change_watch": change_watch,
            }
        )

    _free_last_result = {
        "ok": True,
        "day": day,
        "slots_run": [s["slot"] for s in slot_results],
        "slot_results": slot_results,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "started": _free_scheduler_started,
        "paused": _free_stop_event.is_set(),
        "ran_slots_today": sorted(_free_ran_slots),
        "last_ok": True,
        "last_result": _free_last_result,
        "config": {"enabled": _free_scheduler_enabled(), **schedule_summary()},
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _free_status_path().write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "forced": force, **_free_last_result}


def start_free_source_daily_scheduler() -> bool:
    """Start free-portal daemon for 08:00 + 15:00 local slots (independent of NextBid lock)."""
    global _free_scheduler_started
    with _free_scheduler_lock:
        if _free_scheduler_started:
            return False
        if not _free_scheduler_enabled():
            pause_free_source_scheduler(reason="SCHEDULER_ENABLED_false")
            return False
        _free_stop_event.clear()

        def _loop() -> None:
            time.sleep(20)
            while not _free_stop_event.is_set():
                try:
                    run_scheduled_free_sources_daily(force=False)
                except Exception as exc:  # noqa: BLE001
                    _free_status_path().write_text(
                        json.dumps(
                            {
                                "started": True,
                                "paused": _free_stop_event.is_set(),
                                "last_error": f"{type(exc).__name__}:{exc}"[:240],
                                "at": datetime.now(timezone.utc).isoformat(),
                                "config": {
                                    "enabled": _free_scheduler_enabled(),
                                    **schedule_summary(),
                                },
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                for _ in range(60):
                    if _free_stop_event.is_set():
                        break
                    time.sleep(1)

        th = threading.Thread(target=_loop, name="free-source-slot-scheduler", daemon=True)
        th.start()
        _free_scheduler_started = True
        _free_status_path().write_text(
            json.dumps(
                {
                    "started": True,
                    "paused": False,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "config": {"enabled": _free_scheduler_enabled(), **schedule_summary()},
                    "mode": "free_twice_daily_slots",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return True
