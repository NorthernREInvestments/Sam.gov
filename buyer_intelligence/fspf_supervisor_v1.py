"""Independent FSPF production supervisor — stall detection & auto-recovery.

Runs on a dedicated daemon thread started from the FastAPI lifespan. It does
NOT share the worker's processing thread or Playwright event loop.

Rules (production defaults):
  - Poll every ~12s
  - At 90s without meaningful progress while unfinished work remains → STALL_SUSPECT
  - By 120s after confirmed stall → supersede lease and resume SAME job
  - Rate-limit recoveries to avoid crash loops
  - Heartbeat alone does not excuse zero progress on the same opportunity
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.fspf_supervisor_v1")

BUILD = "20261015-m3-fspf-supervisor-v1"
FAMILY = "full_scale_production_funnel_v1"
STATUS_REL = "_og_commercial_research_pipeline_out/m3_fspf_supervisor_status_v1.json"

POLL_S = float(os.environ.get("M3_FSPF_SUPERVISOR_POLL_S") or "12")
STALL_DETECT_S = float(os.environ.get("M3_FSPF_STALL_DETECT_S") or "90")
STALL_RECOVER_S = float(os.environ.get("M3_FSPF_STALL_RECOVER_S") or "120")
# Max automatic recoveries in a rolling window
MAX_RECOVERIES = int(os.environ.get("M3_FSPF_SUPERVISOR_MAX_RECOVERIES") or "8")
RECOVERY_WINDOW_S = float(os.environ.get("M3_FSPF_SUPERVISOR_RECOVERY_WINDOW_S") or "3600")
# Cooldown after a recovery before another is allowed
RECOVERY_COOLDOWN_S = float(os.environ.get("M3_FSPF_SUPERVISOR_COOLDOWN_S") or "45")
ENABLED = (os.environ.get("M3_FSPF_SUPERVISOR") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict[str, Any] = {
    "kind": "FspfSupervisorStatus",
    "build": BUILD,
    "running": False,
    "phase": "IDLE",
}


def _utc() -> str:
    return now_utc().isoformat()


def _save_status(**kwargs: Any) -> dict[str, Any]:
    with _lock:
        _status.update(kwargs)
        _status["build"] = BUILD
        _status["at"] = _utc()
        _status["heartbeat_at"] = _utc()
        try:
            path = data_path(STATUS_REL)
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            path.write_text(json.dumps(_status, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
        return dict(_status)


def supervisor_status() -> dict[str, Any]:
    with _lock:
        alive = bool(_thread and _thread.is_alive() and not _stop.is_set())
        out = dict(_status)
        out["thread_alive"] = alive
        out["enabled"] = ENABLED
        out["poll_s"] = POLL_S
        out["stall_detect_s"] = STALL_DETECT_S
        out["stall_recover_s"] = STALL_RECOVER_S
        return out


def request_stop() -> dict[str, Any]:
    _stop.set()
    return _save_status(running=False, phase="STOP_REQUESTED")


def ensure_supervisor_running() -> dict[str, Any]:
    """Start the independent supervisor if not already alive."""
    global _thread
    if not ENABLED:
        return {"ok": False, "skipped": True, "reason": "M3_FSPF_SUPERVISOR_disabled"}
    with _lock:
        if _thread and _thread.is_alive() and not _stop.is_set():
            return {"ok": True, "already_running": True, **supervisor_status()}
        _stop.clear()
        sid = f"fspf-sup-{uuid.uuid4().hex[:8]}"
        _save_status(
            running=True,
            phase="STARTING",
            supervisor_id=sid,
            started_at=_utc(),
            ticks=0,
            recoveries=0,
            recovery_times=[],
            last_error=None,
            critical_alert=None,
        )

        def _run() -> None:
            _supervisor_loop(supervisor_id=sid)

        _thread = threading.Thread(target=_run, name=f"fspf-supervisor-{sid}", daemon=True)
        _thread.start()
    return {"ok": True, "started": True, **supervisor_status()}


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _age_s(raw: Any) -> float | None:
    dt = _parse_ts(raw)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _progress_marker(job: dict[str, Any], prog: dict[str, Any]) -> tuple[Any, ...]:
    """Meaningful progress tuple — heartbeat alone is NOT included."""
    return (
        int(job.get("COMPLETED") or prog.get("completed") or 0),
        int(job.get("RESUME_CURSOR") or prog.get("resume_cursor") or 0),
        str(
            job.get("CURRENT_OPPORTUNITY_ID")
            or prog.get("current_opportunity_id")
            or ""
        ),
        str(prog.get("phase") or ""),
        int(job.get("ITEM_COUNT_ON_DISK") or 0)
        if job.get("ITEM_COUNT_ON_DISK") is not None
        else int(prog.get("completed") or 0),
    )


def _job_unfinished(job: dict[str, Any]) -> bool:
    total = int(job.get("TOTAL") or 0)
    completed = int(job.get("COMPLETED") or 0)
    if total <= 0:
        return False
    status = str(job.get("STATUS") or "")
    if status in {"DONE", "CANCELLED"}:
        return False
    return completed < total or status in {
        "RUNNING",
        "STALE_RELEASED",
        "INTERRUPTED",
        "FAILED",
        "ERROR",
        "STOPPED",
        "BLOCKED",
    }


def _trim_recoveries(times: list[float]) -> list[float]:
    cutoff = time.time() - RECOVERY_WINDOW_S
    return [t for t in times if t >= cutoff]


def _supervisor_loop(*, supervisor_id: str) -> None:
    from buyer_intelligence.full_scale_production_funnel_v1 import (
        FAMILY as FSPF_FAMILY,
        PROGRESS_FILE,
        QUEUE_FILE,
        _load,
        start_job,
    )
    from m3_jobs.store import JobStore

    last_marker: tuple[Any, ...] | None = None
    last_marker_at = time.time()
    stall_since: float | None = None
    ticks = 0
    recovery_times: list[float] = []

    _save_status(running=True, phase="WATCHING", supervisor_id=supervisor_id)
    log.info("FSPF supervisor started id=%s", supervisor_id)

    while not _stop.is_set():
        ticks += 1
        try:
            store = JobStore(FSPF_FAMILY)
            # Prefer active pointer / unfinished family job (includes STALE/FAILED)
            job = store.find_active_job(stale_seconds=STALL_DETECT_S) or {}
            if not job or not job.get("JOB_ID"):
                # Scan for unfinished production job
                for p in sorted(
                    store.root.glob("*/job.json"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )[:8]:
                    cand = store.load_job(p.parent.name)
                    if cand and _job_unfinished(cand):
                        job = cand
                        break
            prog = _load(PROGRESS_FILE) or {}
            queue = _load(QUEUE_FILE) or {}
            jid = str(job.get("JOB_ID") or prog.get("job_id") or "")
            if jid and (not job or job.get("JOB_ID") != jid):
                job = store.load_job(jid) or job

            marker = _progress_marker(job or {}, prog)
            now = time.time()
            status = str((job or {}).get("STATUS") or prog.get("status") or "")
            hb_age = _age_s(
                (job or {}).get("HEARTBEAT_AT") or prog.get("heartbeat_at")
            )
            # Already-stale / failed jobs: seed idle clock from heartbeat age so
            # deploy-time recovery does not wait another full 120s blindly.
            if last_marker is None and marker is not None:
                last_marker = marker
                if status in {
                    "STALE_RELEASED",
                    "FAILED",
                    "ERROR",
                    "INTERRUPTED",
                    "STOPPED",
                } and hb_age is not None:
                    last_marker_at = now - float(hb_age)
                else:
                    last_marker_at = now
                stall_since = None
            elif marker != last_marker:
                last_marker = marker
                last_marker_at = now
                stall_since = None
            elif _job_unfinished(job or {}) or (
                str(prog.get("status") or "").upper() == "RUNNING"
                and int((queue or {}).get("eligible_total") or job.get("TOTAL") or 0)
                > int(job.get("COMPLETED") or prog.get("completed") or 0)
            ):
                idle_s = now - last_marker_at
                # Heartbeat-stale RUNNING with no progress also counts.
                if hb_age is not None and hb_age > idle_s:
                    idle_s = hb_age
                if idle_s >= STALL_DETECT_S:
                    if stall_since is None:
                        stall_since = last_marker_at + STALL_DETECT_S
                    _save_status(
                        running=True,
                        phase="STALL_SUSPECT",
                        ticks=ticks,
                        job_id=jid or None,
                        stall_idle_s=round(idle_s, 1),
                        last_marker=list(marker),
                        worker_id=job.get("WORKER_ID") or prog.get("worker_id"),
                        job_status=job.get("STATUS"),
                        progress_status=prog.get("status"),
                    )
                    if idle_s >= STALL_RECOVER_S:
                        recovery_times = _trim_recoveries(recovery_times)
                        last_rec = recovery_times[-1] if recovery_times else 0.0
                        if now - last_rec < RECOVERY_COOLDOWN_S:
                            _save_status(
                                phase="RECOVERY_COOLDOWN",
                                ticks=ticks,
                                cooldown_remaining_s=round(
                                    RECOVERY_COOLDOWN_S - (now - last_rec), 1
                                ),
                            )
                        elif len(recovery_times) >= MAX_RECOVERIES:
                            _save_status(
                                phase="CRITICAL_ALERT",
                                critical_alert=(
                                    f"recovery_rate_exceeded:{len(recovery_times)}"
                                    f"_in_{int(RECOVERY_WINDOW_S)}s"
                                ),
                                ticks=ticks,
                                job_id=jid or None,
                            )
                            log.error(
                                "FSPF supervisor CRITICAL: recovery loop job=%s count=%s",
                                jid,
                                len(recovery_times),
                            )
                        elif jid:
                            ok = _recover_job(
                                store=store,
                                job_id=jid,
                                supervisor_id=supervisor_id,
                                start_job=start_job,
                                reason=(
                                    f"no_progress_{int(idle_s)}s"
                                    f"_status={job.get('STATUS')}"
                                ),
                            )
                            recovery_times.append(now)
                            _save_status(
                                phase="RECOVERED" if ok.get("ok") else "RECOVERY_FAILED",
                                ticks=ticks,
                                recoveries=len(recovery_times),
                                recovery_times=recovery_times[-MAX_RECOVERIES:],
                                last_recovery=ok,
                                last_error=None if ok.get("ok") else ok.get("error"),
                            )
                            # Reset marker clock so we wait for new worker progress
                            last_marker = None
                            last_marker_at = time.time()
                            stall_since = None
                else:
                    _save_status(
                        running=True,
                        phase="WATCHING",
                        ticks=ticks,
                        job_id=jid or None,
                        idle_s=round(idle_s, 1),
                        last_marker=list(marker),
                        worker_id=job.get("WORKER_ID") or prog.get("worker_id"),
                        job_status=job.get("STATUS"),
                        queue_total=(queue or {}).get("eligible_total")
                        or job.get("TOTAL"),
                        completed=job.get("COMPLETED") or prog.get("completed"),
                    )
            else:
                _save_status(
                    running=True,
                    phase="IDLE_OR_DONE",
                    ticks=ticks,
                    job_id=jid or None,
                    job_status=(job or {}).get("STATUS"),
                )
        except Exception as exc:
            log.exception("FSPF supervisor tick failed")
            _save_status(
                running=True,
                phase="TICK_ERROR",
                ticks=ticks,
                last_error=f"{type(exc).__name__}:{exc}"[:240],
            )

        _stop.wait(POLL_S)

    _save_status(running=False, phase="STOPPED", ticks=ticks)
    log.info("FSPF supervisor stopped id=%s", supervisor_id)


def _recover_job(
    *,
    store: Any,
    job_id: str,
    supervisor_id: str,
    start_job: Any,
    reason: str,
) -> dict[str, Any]:
    """Supersede lease and resume the SAME job without queue rebuild."""
    before = store.load_job(job_id) or {}
    cursor_before = int(before.get("RESUME_CURSOR") or 0)
    completed_before = int(before.get("COMPLETED") or 0)
    try:
        store.force_reset_running_lock(job_id)
    except Exception as exc:
        return {"ok": False, "error": f"force_reset:{type(exc).__name__}:{exc}"}

    # Mark prior worker superseded with generation bump via acquire in start_job
    try:
        result = start_job(
            force=True,
            rebuild_queue=False,
            new_run=False,
            job_id=job_id,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"start_job:{type(exc).__name__}:{exc}",
            "cursor_before": cursor_before,
            "completed_before": completed_before,
        }

    after = store.load_job(job_id) or {}
    out = {
        "ok": bool(result.get("ok")),
        "reason": reason,
        "supervisor_id": supervisor_id,
        "at": _utc(),
        "job_id": job_id,
        "cursor_before": cursor_before,
        "cursor_after": int(after.get("RESUME_CURSOR") or 0),
        "completed_before": completed_before,
        "completed_after": int(after.get("COMPLETED") or 0),
        "new_worker_id": result.get("worker_id") or after.get("WORKER_ID"),
        "lease_generation": after.get("LEASE_GENERATION"),
        "start_status": result.get("status"),
        "start_error": result.get("error"),
    }
    log.warning(
        "FSPF supervisor recovery job=%s reason=%s worker=%s cursor=%s→%s",
        job_id,
        reason,
        out.get("new_worker_id"),
        cursor_before,
        out.get("cursor_after"),
    )
    return out


def supervisor_tick_once_for_tests() -> dict[str, Any]:
    """Synchronous single evaluation for unit tests (does not start the loop)."""
    ensure_supervisor_running()
    return supervisor_status()
