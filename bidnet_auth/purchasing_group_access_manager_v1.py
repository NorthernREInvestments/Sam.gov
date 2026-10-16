"""Independent purchasing-group access manager.

Runs alongside the first-pass opportunity funnel with its own BidNet browser
client and strict concurrency (max 1 registration at a time). Does not share
Playwright resources with opportunity workers and never resets the funnel queue.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.bidnet_pg_access_manager")

BUILD = "20261016-m3-bidnet-pg-access-manager-v1j"
STATUS_REL = "_og_commercial_research_pipeline_out/m3_pg_access_manager_status_v1.json"

# Idle pause between registration attempts (keeps opportunity lane uncontested)
IDLE_SLEEP_S = float(os.environ.get("M3_PG_ACCESS_IDLE_S") or "12")
# Wait long enough that opportunity-lane item gaps can yield the mutex.
BROWSER_SLOT_WAIT_S = float(os.environ.get("M3_PG_ACCESS_BROWSER_WAIT_S") or "45")
# Hard ceiling per registration slice call
SLICE_MAX_GROUPS = int(os.environ.get("M3_PG_ACCESS_GROUPS_PER_TICK") or "1")
# Stop manager when opportunity worker requests stop
_ENABLED = (os.environ.get("M3_PG_ACCESS_MANAGER") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict[str, Any] = {
    "kind": "PurchasingGroupAccessManager",
    "build": BUILD,
    "running": False,
    "worker_id": None,
    "job_id": None,
}


def _utc() -> str:
    return now_utc().isoformat()


def _save_status(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    global _status
    with _lock:
        if patch:
            _status = {**_status, **patch, "updated_at": _utc(), "build": BUILD}
        else:
            _status = {**_status, "updated_at": _utc(), "build": BUILD}
        snap = deepcopy(_status)
    try:
        path = data_path(STATUS_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
    return snap


def manager_status() -> dict[str, Any]:
    with _lock:
        return deepcopy(_status)


def is_running() -> bool:
    t = _thread
    return bool(t and t.is_alive() and not _stop.is_set())


def request_stop() -> dict[str, Any]:
    _stop.set()
    return _save_status({"running": False, "phase": "STOP_REQUESTED"})


def ensure_access_manager_running(*, job_id: str | None = None) -> dict[str, Any]:
    """Start the independent registration lane if not already alive."""
    global _thread
    if not _ENABLED:
        return {"ok": False, "skipped": True, "reason": "M3_PG_ACCESS_MANAGER_disabled"}
    with _lock:
        if _thread and _thread.is_alive() and not _stop.is_set():
            if job_id:
                _status["job_id"] = job_id
            return {"ok": True, "already_running": True, **manager_status()}
        _stop.clear()
        worker_id = f"pg-access-{uuid.uuid4().hex[:8]}"
        _save_status(
            {
                "running": True,
                "phase": "STARTING",
                "worker_id": worker_id,
                "job_id": job_id,
                "started_at": _utc(),
                "ticks": 0,
                "groups_attempted": 0,
                "groups_completed": 0,
                "last_error": None,
            }
        )

        def _run() -> None:
            _manager_loop(job_id=job_id, worker_id=worker_id)

        _thread = threading.Thread(
            target=_run, name=f"pg-access-manager-{worker_id}", daemon=True
        )
        _thread.start()
    return {"ok": True, "started": True, **manager_status()}


def _manager_loop(*, job_id: str | None, worker_id: str) -> None:
    from buyer_intelligence.commercial_funnel_execution_v1 import _open_bidnet_auth_client
    from bidnet_auth.purchasing_group_queue import process_group_registration_slice
    from bidnet_auth.purchasing_group_tracker import clear_auto_fixable_owner_blockers

    auth_client = None
    consecutive_auth_fail = 0
    try:
        while not _stop.is_set():
            active_job = job_id
            if not active_job:
                try:
                    from m3_jobs.store import JobStore

                    active = JobStore("full_scale_production_funnel_v1").find_active_job(
                        stale_seconds=3600
                    )
                    if active and active.get("JOB_ID"):
                        active_job = str(active["JOB_ID"])
                except Exception:
                    active_job = job_id

            _save_status(
                {
                    "running": True,
                    "phase": "TICK",
                    "job_id": active_job,
                    "worker_id": worker_id,
                    "heartbeat_at": _utc(),
                }
            )

            try:
                clear_auto_fixable_owner_blockers()
            except Exception:
                pass

            # Serialize Playwright against the opportunity lane (same session store).
            from bidnet_auth.browser_mutex_v1 import bidnet_browser_slot

            slice_report: dict[str, Any] | None = None
            with bidnet_browser_slot(
                holder=worker_id, timeout_s=BROWSER_SLOT_WAIT_S, ttl_s=240.0
            ) as got_browser:
                if not got_browser:
                    _save_status(
                        {
                            "phase": "WAIT_BROWSER_SLOT",
                            "heartbeat_at": _utc(),
                            "note": "opportunity_lane_holds_playwright",
                            "browser_wait_s": BROWSER_SLOT_WAIT_S,
                        }
                    )
                    # Short poll — FSPF yields ~4s between opportunity items.
                    _stop.wait(min(3.0, IDLE_SLEEP_S))
                    continue

                # Dedicated auth client for this registration tick only
                if auth_client is not None:
                    try:
                        auth_client.close()
                    except Exception:
                        pass
                    auth_client = None
                try:
                    auth_status, auth_client = _open_bidnet_auth_client()
                except Exception as exc:
                    auth_status = {"authenticated": False, "error": type(exc).__name__}
                    auth_client = None
                if not (auth_status or {}).get("authenticated") or auth_client is None:
                    consecutive_auth_fail += 1
                    _save_status(
                        {
                            "phase": "AUTH_WAIT",
                            "last_error": (auth_status or {}).get("error")
                            or (auth_status or {}).get("status")
                            or "BIDNET_AUTH_REQUIRED",
                            "auth_fail_streak": consecutive_auth_fail,
                        }
                    )
                    _stop.wait(min(60.0, IDLE_SLEEP_S * (1 + consecutive_auth_fail)))
                    continue
                consecutive_auth_fail = 0

                try:
                    slice_report = process_group_registration_slice(
                        auth_client,
                        max_groups=SLICE_MAX_GROUPS,
                        job_id=active_job,
                    )
                except Exception as exc:
                    log.exception("pg access manager slice failed")
                    slice_report = {"error": type(exc).__name__, "message": str(exc)[:160]}
                finally:
                    if auth_client is not None:
                        try:
                            auth_client.close()
                        except Exception:
                            pass
                        auth_client = None

            processed = list((slice_report or {}).get("processed") or [])
            completed_n = sum(
                1
                for r in processed
                if str(r.get("status") or "") == "REGISTERED" or r.get("already_registered")
            )
            with _lock:
                ticks = int(_status.get("ticks") or 0) + 1
                attempted = int(_status.get("groups_attempted") or 0) + len(processed)
                done = int(_status.get("groups_completed") or 0) + completed_n
            _save_status(
                {
                    "phase": "IDLE" if not processed else "PROCESSED",
                    "ticks": ticks,
                    "groups_attempted": attempted,
                    "groups_completed": done,
                    "last_slice": {
                        "processed": processed[:5],
                        "pending_remaining": (slice_report or {}).get("pending_remaining"),
                        "opportunities_released": (slice_report or {}).get(
                            "opportunities_released"
                        ),
                        "owner_auto_cleared": (slice_report or {}).get("owner_auto_cleared"),
                        "error": (slice_report or {}).get("error"),
                    },
                    "heartbeat_at": _utc(),
                }
            )

            pending_left = int((slice_report or {}).get("pending_remaining") or 0)
            _stop.wait(max(IDLE_SLEEP_S, 20.0) if pending_left <= 0 else IDLE_SLEEP_S)
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass
        _save_status({"running": False, "phase": "STOPPED", "stopped_at": _utc()})


def rank_groups_by_unlock_impact(limit: int = 20) -> list[dict[str, Any]]:
    """Rank pending/tracked groups by blocked live opportunity count."""
    from bidnet_auth.purchasing_group_queue import load_queue
    from bidnet_auth.purchasing_group_tracker import load_tracker

    q = load_queue()
    tracker = load_tracker()
    groups = tracker.get("groups") or {}
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list(q.get("pending") or []) + list(q.get("failed") or []):
        pg = str(row.get("purchasing_group_id") or "")
        if not pg or pg in seen:
            continue
        seen.add(pg)
        g = groups.get(pg) or {}
        oids = max(
            len(row.get("opportunity_ids") or []),
            len(g.get("blocked_opportunity_ids") or []),
            int(g.get("live_eligible_opportunity_count") or 0),
        )
        ranked.append(
            {
                "purchasing_group_id": pg,
                "name": row.get("name") or g.get("name"),
                "status": g.get("status") or row.get("status"),
                "blocked_opportunities": oids,
                "owner_blocker_reason": g.get("owner_blocker_reason"),
                "missing_fields": (g.get("missing_fields") or [])[:4],
                "attempts": row.get("attempts"),
            }
        )
    ranked.sort(key=lambda r: -int(r.get("blocked_opportunities") or 0))
    return ranked[:limit]
