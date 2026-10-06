"""Async Full-100 throughput job — start returns immediately; poll status.

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from full100_throughput.models import BUILD, CK, JOB, REPORT
from m3_data_root import data_path

log = logging.getLogger("govtracker.full100_throughput.job")

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_runner_lock = threading.Lock()


def _utc() -> str:
    return now_utc().isoformat()


def _persist_job(job: dict[str, Any]) -> None:
    p = data_path(JOB)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")


def _set(job_id: str, **patch: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(patch)
        job["updated_at"] = _utc()
        _persist_job(job)


def get_job(job_id: str | None = None) -> dict[str, Any] | None:
    with _lock:
        if job_id and job_id in _jobs:
            return deepcopy(_jobs[job_id])
        if _jobs:
            latest = sorted(_jobs.values(), key=lambda j: str(j.get("started_at") or ""), reverse=True)[0]
            return deepcopy(latest)
    p = data_path(JOB)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def start_full100_job(*, resume: bool = True, workers: int = 4) -> dict[str, Any]:
    """Start background Full-100 completion. Returns immediately."""
    with _lock:
        for j in _jobs.values():
            if j.get("status") == "RUNNING":
                return {"ok": True, "already_running": True, "job": deepcopy(j)}
        job_id = f"F100-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        job = {
            "job_id": job_id,
            "build": BUILD,
            "status": "RUNNING",
            "started_at": _utc(),
            "updated_at": _utc(),
            "progress": {"phase": "STARTING", "pct": 0, "completed": 0, "remaining": 0, "active": 0},
            "resume": resume,
            "workers": workers,
            "error": None,
            "result_path": REPORT,
            "checkpoint_path": CK,
        }
        _jobs[job_id] = job
        _persist_job(job)

    def _run() -> None:
        if not _runner_lock.acquire(blocking=False):
            _set(job_id, status="FAILED", error="ANOTHER_RUNNER_ACTIVE", completed_at=_utc())
            return
        try:
            from full100_throughput.sweep import run_full100_throughput_v1

            def on_progress(**kw: Any) -> None:
                _set(
                    job_id,
                    progress={
                        "phase": kw.get("phase"),
                        "pct": kw.get("pct"),
                        "completed": kw.get("completed"),
                        "remaining": kw.get("remaining"),
                        "active": kw.get("active"),
                        "n": kw.get("n"),
                        "eta_s": kw.get("eta_s"),
                    },
                )

            report = run_full100_throughput_v1(
                resume=resume,
                workers=workers,
                on_progress=on_progress,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                summary={
                    "full100_pass": (report.get("full100") or {}).get("pass"),
                    "coverage": (report.get("full100") or {}).get("coverage"),
                    "accuracy": (report.get("full100") or {}).get("accuracy"),
                    "safe_to_scale": report.get("safe_to_scale"),
                },
            )
        except Exception as exc:
            log.exception("Full-100 throughput job failed")
            _set(job_id, status="FAILED", error=str(exc), completed_at=_utc())
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"full100-{job_id}", daemon=True).start()
    return {"ok": True, "job_id": job_id, "job": get_job(job_id)}
