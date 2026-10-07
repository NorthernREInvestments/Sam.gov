"""Background BidNet/OpenGov harvest jobs — return immediately; poll for status.

Long Playwright work must not block HTTP proxies or Cursor terminals.
Only one Playwright auth job runs at a time (single-flight) to avoid browser deadlocks.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_auth_jobs")

_lock = threading.Lock()
_runner_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
JOB_FILE = "auth_jobs/last_job_status.json"
STALE_SECONDS = 90 * 60  # national ~900 pages + state fill-in; heartbeats via progress
E2E_STALE_SECONDS = 180 * 60  # full production E2E can exceed 90m with harvest+universe+recovery
FFS_STALE_SECONDS = 720 * 60  # full funnel sweep: discovery + universe free-package batches + economics
BASELINE_STALE_SECONDS = 36 * 3600  # one-time BidNet baseline ~24–30h with checkpoint heartbeats
MONEY_STALE_SECONDS = 6 * 3600  # focused money sprint


def _utc() -> str:
    return now_utc().isoformat()


def _persist(job: dict[str, Any]) -> None:
    try:
        from m3_data_root import data_path

        path = data_path(JOB_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(job, indent=2, default=str)
        path.write_text(payload, encoding="utf-8")
        jid = str(job.get("job_id") or "").strip()
        if jid:
            per = data_path(f"auth_jobs/{jid}.json")
            per.parent.mkdir(parents=True, exist_ok=True)
            per.write_text(payload, encoding="utf-8")
    except Exception:
        log.exception("Failed persisting auth job status")


def _load_persisted_job(job_id: str) -> dict[str, Any] | None:
    try:
        from m3_data_root import data_path

        path = data_path(f"auth_jobs/{job_id}.json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        latest = data_path(JOB_FILE)
        if latest.exists():
            row = json.loads(latest.read_text(encoding="utf-8"))
            if str(row.get("job_id") or "") == job_id:
                return row
    except Exception:
        log.exception("Failed loading persisted auth job %s", job_id)
    return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _fail_stale_locked() -> None:
    """Mark RUNNING jobs with no heartbeat as FAILED (caller holds _lock)."""
    now = now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for job in _jobs.values():
        if job.get("status") != "RUNNING":
            continue
        ts = _parse_ts(job.get("updated_at") or job.get("started_at"))
        if not ts:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()
        kind = str(job.get("kind") or "")
        if kind == "bidnet_baseline_production":
            limit = BASELINE_STALE_SECONDS
        elif kind == "bidnet_money_path":
            limit = MONEY_STALE_SECONDS
        elif kind == "channel_fit_canary":
            limit = MONEY_STALE_SECONDS
        elif kind == "schedule_backed_canary":
            limit = MONEY_STALE_SECONDS
        elif kind == "full_funnel_sweep":
            limit = FFS_STALE_SECONDS
        elif kind == "full_production_e2e":
            limit = E2E_STALE_SECONDS
        else:
            limit = STALE_SECONDS
        if age > limit:
            job["status"] = "FAILED"
            job["error"] = "STALE_TIMEOUT"
            job["completed_at"] = _utc()
            job["updated_at"] = _utc()
            job["progress"] = {"phase": "FAILED", "pct": 100}
            _persist(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        _fail_stale_locked()
        job = _jobs.get(job_id)
        if job:
            return deepcopy(job)
    persisted = _load_persisted_job(job_id)
    if persisted:
        # Surface disk state after process restart (do not revive dead RUNNING threads).
        if persisted.get("status") == "RUNNING":
            persisted = dict(persisted)
            persisted["status"] = "FAILED"
            persisted["error"] = persisted.get("error") or "PROCESS_RESTARTED"
            persisted["completed_at"] = persisted.get("completed_at") or _utc()
            persisted["progress"] = {"phase": "FAILED", "pct": 100, "note": "PROCESS_RESTARTED"}
        return persisted
    return None


def latest_job() -> dict[str, Any] | None:
    with _lock:
        _fail_stale_locked()
        if not _jobs:
            try:
                from m3_data_root import data_path

                path = data_path(JOB_FILE)
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            return None
        jobs = sorted(_jobs.values(), key=lambda j: str(j.get("started_at") or ""), reverse=True)
        return deepcopy(jobs[0]) if jobs else None


def _set(job_id: str, **patch: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(patch)
        job["updated_at"] = _utc()
        _persist(job)


def _active_running_id() -> str | None:
    with _lock:
        _fail_stale_locked()
        for job in sorted(_jobs.values(), key=lambda j: str(j.get("started_at") or ""), reverse=True):
            if job.get("status") == "RUNNING":
                return str(job.get("job_id"))
    return None


def start_bidnet_harvest_job(
    *,
    max_results: int = 100,
    max_pages: int = 8,
    open_details: bool = False,
    detail_limit: int | None = None,
    persist: bool = True,
    mode: str | None = None,
) -> dict[str, Any]:
    # Full-universe runs use state partitions (national open-bids soft-caps ~6–7k).
    use_partitioned = (mode or "").lower() in {"partitioned", "full", "state"} or int(max_results) >= 7000
    job_id = f"BNH-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_partitioned_harvest" if use_partitioned else "bidnet_harvest",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "max_results": max_results,
            "max_pages": max_pages,
            "open_details": open_details,
            "detail_limit": detail_limit,
            "persist": persist,
            "mode": "partitioned" if use_partitioned else "authenticated_search",
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 5})
        phase0 = "PARTITIONED_HARVEST" if use_partitioned else "AUTH_HARVEST"

        def _progress(**kwargs: Any) -> None:
            phase = str(kwargs.get("phase") or phase0)
            pct = kwargs.get("pct")
            if pct is None:
                pct = 10
            _set(
                job_id,
                status="RUNNING",
                progress={
                    "phase": phase,
                    "pct": int(pct),
                    "pages": kwargs.get("pages"),
                    "retrieved": kwargs.get("retrieved"),
                    "partition": kwargs.get("partition"),
                    "partitions_done": kwargs.get("partitions_done"),
                    "reported": kwargs.get("reported"),
                },
            )

        def _execute() -> None:
            _set(job_id, status="RUNNING", progress={"phase": phase0, "pct": 10})
            if use_partitioned:
                from bidnet_discovery import run_bidnet_partitioned_harvest

                # Skip Playwright auth seed for partitioned HTTP harvest — national
                # list reports the UI total, and auth seed previously hung at pct=10.
                result = run_bidnet_partitioned_harvest(
                    max_results=max_results,
                    max_pages_per_partition=min(max(max_pages, 400), 1200),
                    include_national=True,
                    persist=persist,
                    run_id=job_id,
                    use_auth_seed=False,
                    on_progress=_progress,
                )
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "mode": "partitioned",
                        "partition_method": result.get("partition_method"),
                        "reported_open_ui": result.get("reported_open_ui"),
                        "retrieved_unique": result.get("retrieved_unique"),
                        "retrieval_pct": result.get("retrieval_pct"),
                        "pagination_complete": result.get("pagination_complete"),
                        "remaining_gap": result.get("remaining_gap"),
                        "partitions": result.get("partitions"),
                        "canonical_merge": result.get("canonical_merge"),
                        "net_new": result.get("net_new"),
                        "DISCOVERY_TRUNCATED": result.get("DISCOVERY_TRUNCATED"),
                        "auth": (result.get("auth") or {}).get("status")
                        if isinstance(result.get("auth"), dict)
                        else result.get("auth"),
                    },
                )
            else:
                from bidnet_discovery import run_bidnet_authenticated_harvest

                result = run_bidnet_authenticated_harvest(
                    max_results=max_results,
                    max_pages=max_pages,
                    open_details=open_details,
                    detail_limit=detail_limit,
                    persist=persist,
                    run_id=job_id,
                    use_auth=True,
                    on_progress=_progress,
                )
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "mode": "authenticated_search",
                        "auth": (result.get("auth") or {}).get("status")
                        if isinstance(result.get("auth"), dict)
                        else result.get("auth"),
                        "harvest": result.get("harvest"),
                        "canonical_merge": result.get("canonical_merge"),
                        "blocker": result.get("blocker"),
                        "records_sample": result.get("records_sample"),
                    },
                )

        try:
            # Partitioned harvest is mostly HTTP — do not block OpenGov/Euna Playwright slot
            # for the full national pagination window. Auth seed still uses Playwright briefly
            # inside the harvest; single-flight only required for pure Playwright jobs.
            if use_partitioned:
                _execute()
            else:
                with _runner_lock:
                    _execute()
        except Exception as exc:
            log.exception("BidNet harvest job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=type(exc).__name__,
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"bidnet-harvest-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "mode": "partitioned" if use_partitioned else "authenticated_search",
        "active_running": _active_running_id(),
    }


def start_bidnet_gap_closure_job() -> dict[str, Any]:
    """Retry only the missing BidNet inventory. Does not re-harvest the closed 21,969."""
    job_id = f"BNG-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_gap_closure",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "reported_open": 24372,
            "harvested": 21969,
            "missing": 2403,
            "retune_threshold": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        def _progress(**kwargs: Any) -> None:
            _set(
                job_id,
                status="RUNNING",
                progress={
                    "phase": str(kwargs.get("phase") or "GAP_RETRY"),
                    "pct": int(kwargs.get("pct") or 0),
                    "partition": kwargs.get("partition"),
                    "pages": kwargs.get("pages"),
                    "retrieved": kwargs.get("retrieved"),
                },
            )

        try:
            _progress(phase="LOAD", pct=1)
            from bidnet_gap_closure.run import run_bidnet_gap_closure

            result = run_bidnet_gap_closure(on_progress=_progress)
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_DISCOVERY_TRULY_COMPLETE": result.get("BIDNET_DISCOVERY_TRULY_COMPLETE"),
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "classification_counts": result.get("classification_counts"),
                    "recovery": result.get("recovery"),
                    "true_coverage": result.get("true_coverage"),
                    "conservation": result.get("conservation"),
                    "gates": result.get("gates"),
                    "raw": result.get("raw"),
                },
            )
        except Exception as exc:
            log.exception("BidNet gap closure failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"bidnet-gap-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_gap_closure",
        "active_running": _active_running_id(),
    }


def start_bidnet_engine_job(
    *,
    scale_sample_size: int = 8,
    baseline_batch: int = 60,
    stress_n: int = 5000,
    time_budget_s: int = 2400,
) -> dict[str, Any]:
    """Parallel/incremental BidNet engine. Preserves prior 120. Uses playwright slot."""
    job_id = f"BNE-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_engine",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "scale_sample_size": int(scale_sample_size),
            "baseline_batch": int(baseline_batch),
            "stress_n": int(stress_n),
            "time_budget_s": int(time_budget_s),
            "sam_calls": 0,
            "rerun_discovery": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "ENGINE"),
                        "pct": int(kwargs.get("pct") or 0),
                        "workers": kwargs.get("workers"),
                        "completed": kwargs.get("completed"),
                        "total": kwargs.get("total"),
                        "rate": kwargs.get("rate"),
                        "throughput": kwargs.get("throughput"),
                    },
                )

            from bidnet_engine.run import run_bidnet_engine

            result = run_bidnet_engine(
                scale_sample_size=int(scale_sample_size),
                baseline_batch=int(baseline_batch),
                stress_n=int(stress_n),
                time_budget_s=int(time_budget_s),
                on_progress=_progress,
            )
            try:
                from m3_data_root import data_path

                path = data_path("m3_bidnet_engine_v1_last_report.json")
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    saved["run_id"] = job_id
                    path.write_text(json.dumps(saved, indent=2, default=str), encoding="utf-8")
                    from bidnet_engine.run import format_engine_report

                    data_path("m3_bidnet_engine_v1_last_report.txt").write_text(
                        format_engine_report(saved),
                        encoding="utf-8",
                    )
            except Exception:
                log.exception("BidNet engine report patch failed")
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_INCREMENTAL_PARALLEL_PASS": result.get("BIDNET_INCREMENTAL_PARALLEL_PASS"),
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "selected_workers": result.get("selected_workers"),
                    "after": result.get("after"),
                    "baseline": result.get("baseline"),
                    "concurrency": result.get("concurrency"),
                    "stress_5000": {
                        k: (result.get("stress_5000") or {}).get(k)
                        for k in ("input", "conservation_diff", "runtime_s", "deep_processing_required")
                    },
                    "gates": result.get("gates"),
                    "answers": result.get("answers"),
                    "runtime_s": result.get("runtime_s"),
                },
            )
        except Exception as exc:
            log.exception("BidNet engine job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bidnet-engine-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_engine",
        "active_running": _active_running_id(),
    }


def start_schedule_backed_canary_job(
    *,
    canary_n: int = 20,
    price_budget: int = 25,
) -> dict[str, Any]:
    """Run schedule-backed product canary (20). Does not expand to 100."""
    job_id = f"SBC-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "schedule_backed_canary",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "canary_n": int(canary_n),
            "price_budget": int(price_budget),
            "expand_forbidden": True,
            "sam_calls": 0,
            "rerun_discovery": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run_sbc() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "SCHEDULE_BACKED_CANARY"),
                        "pct": int(kwargs.get("pct") or 0),
                        "completed": kwargs.get("completed"),
                    },
                )

            from bidnet_engine.schedule_backed_canary import run_schedule_backed_canary

            result = run_schedule_backed_canary(
                canary_n=int(canary_n),
                price_budget=int(price_budget),
                on_progress=_progress,
            )
            gates = result.get("gates") or {}
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "SCHEDULE_BACKED_PRODUCT_CANARY_PASS": gates.get("SCHEDULE_BACKED_PRODUCT_CANARY_PASS"),
                    "gates": gates,
                    "line_extraction": result.get("line_extraction"),
                    "identity": result.get("identity"),
                    "public_pricing": result.get("public_pricing"),
                    "REAL_LIVE_CALL_TODAY": result.get("REAL_LIVE_CALL_TODAY"),
                    "NEXT_TRUE_BOTTLENECK": result.get("NEXT_TRUE_BOTTLENECK"),
                    "runtime_s": result.get("runtime_s"),
                    "EXPAND_TO_100": "NO",
                },
            )
        except Exception as exc:
            log.exception("Schedule-backed canary job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run_sbc, name=f"schedule-backed-canary-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "schedule_backed_canary",
        "active_running": _active_running_id(),
    }


def start_channel_fit_canary_job(
    *,
    canary_n: int = 20,
    expand_n: int = 100,
    price_budget: int = 25,
) -> dict[str, Any]:
    """Terminate stuck money sprint; run real live channel-fit canary (20, expand only if CALL_TODAY≥1)."""
    job_id = f"CFC-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "channel_fit_canary",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "canary_n": int(canary_n),
            "expand_n": int(expand_n),
            "price_budget": int(price_budget),
            "terminate_job": "MNY-0525b77c8bf6",
            "sam_calls": 0,
            "rerun_discovery": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        # Mark old money job terminated if still queued/running in memory
        old = _jobs.get("MNY-0525b77c8bf6")
        if old and old.get("status") in {"QUEUED", "RUNNING", "SELECTED"}:
            old["status"] = "TERMINATED_NO_PROGRESS"
            old["error"] = "TERMINATED_NO_PROGRESS"
            old["completed_at"] = _utc()
            old["updated_at"] = _utc()
            _persist(old)
        stalled = _jobs.get("BNP-97657480a0d0")
        if stalled and stalled.get("status") == "RUNNING":
            stalled["status"] = "TERMINATED_STALLED"
            stalled["error"] = "STALLED_NO_HEARTBEAT_NO_PROGRESS"
            stalled["completed_at"] = _utc()
            stalled["updated_at"] = _utc()
            _persist(stalled)
        _jobs[job_id] = job
        _persist(job)

    def _run_cfc() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "CHANNEL_FIT_CANARY"),
                        "pct": int(kwargs.get("pct") or 0),
                        "completed": kwargs.get("completed"),
                    },
                )

            from bidnet_engine.channel_fit_canary import run_channel_fit_canary

            result = run_channel_fit_canary(
                canary_n=int(canary_n),
                expand_n=int(expand_n),
                price_budget=int(price_budget),
                on_progress=_progress,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "REAL_LIVE_CALL_TODAY": result.get("REAL_LIVE_CALL_TODAY"),
                    "EXPAND_TO_100": result.get("EXPAND_TO_100"),
                    "blocker": result.get("blocker"),
                    "PRE_QUOTE": result.get("PRE_QUOTE"),
                    "top_opportunity": result.get("top_opportunity"),
                    "runtime_s": result.get("runtime_s"),
                    "FIXTURE_LEAKAGE": result.get("FIXTURE_LEAKAGE"),
                },
            )
        except Exception as exc:
            log.exception("Channel-fit canary job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run_cfc, name=f"channel-fit-canary-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "channel_fit_canary",
        "active_running": _active_running_id(),
    }


def start_bidnet_money_path_job(
    *,
    canary_n: int = 20,
    sprint_n: int = 100,
    max_n: int = 250,
) -> dict[str, Any]:
    """Terminate stalled baseline canary, run real downstream money sprint."""
    job_id = f"MNY-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_money_path",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "canary_n": int(canary_n),
            "sprint_n": int(sprint_n),
            "max_n": int(max_n),
            "stalled_job": "BNP-97657480a0d0",
            "sam_calls": 0,
            "rerun_discovery": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        # Clear stalled baseline record so lock can be acquired after redeploy kill.
        stalled = _jobs.get("BNP-97657480a0d0")
        if stalled and stalled.get("status") == "RUNNING":
            stalled["status"] = "TERMINATED_STALLED"
            stalled["error"] = "STALLED_NO_HEARTBEAT_NO_PROGRESS"
            stalled["completed_at"] = _utc()
            stalled["updated_at"] = _utc()
            _persist(stalled)
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "MONEY"),
                        "pct": int(kwargs.get("pct") or 0),
                        "completed": kwargs.get("completed"),
                    },
                )

            from bidnet_engine.money_path import run_money_sprint

            result = run_money_sprint(
                canary_n=int(canary_n),
                sprint_n=int(sprint_n),
                max_n=int(max_n),
                on_progress=_progress,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "MONEY_SPRINT_PASS": result.get("MONEY_SPRINT_PASS"),
                    "CANARY_PASS": (result.get("canary_20") or {}).get("CANARY_PASS"),
                    "daily_kpi": result.get("daily_kpi"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "actionable": len(result.get("actionable_now") or []),
                    "quote_ready": len(result.get("ready_for_quote") or []),
                    "stalled_job": result.get("stalled_job"),
                    "runtime_s": result.get("runtime_s"),
                },
            )
        except Exception as exc:
            log.exception("BidNet money path job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bidnet-money-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_money_path",
        "active_running": _active_running_id(),
    }


def start_bidnet_baseline_production_job(
    *,
    canary_s: int = 3600,
    baseline_budget_s: int = 30 * 3600,
    skip_canary: bool = False,
) -> dict[str, Any]:
    """Canary-gated one-time baseline, then freeze + incremental production activation."""
    job_id = f"BNP-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_baseline_production",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "canary_s": int(canary_s),
            "baseline_budget_s": int(baseline_budget_s),
            "skip_canary": bool(skip_canary),
            "logical_workers": 5,
            "browser_workers": 2,
            "sam_calls": 0,
            "rerun_discovery": False,
            "full_universe_deep_forbidden_after_baseline": True,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "BASELINE"),
                        "pct": int(kwargs.get("pct") or 0),
                        "workers": kwargs.get("workers"),
                        "browser_workers": kwargs.get("browser_workers"),
                        "completed": kwargs.get("completed"),
                        "remaining": kwargs.get("remaining"),
                        "rate": kwargs.get("rate"),
                    },
                )

            from bidnet_engine.production import run_bidnet_baseline_production

            result = run_bidnet_baseline_production(
                canary_s=int(canary_s),
                baseline_budget_s=int(baseline_budget_s),
                skip_canary=bool(skip_canary),
                on_progress=_progress,
            )
            try:
                from m3_data_root import data_path

                path = data_path("m3_bidnet_production_v1_last_report.json")
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    saved["job_id"] = job_id
                    path.write_text(json.dumps(saved, indent=2, default=str), encoding="utf-8")
            except Exception:
                log.exception("BidNet baseline production report patch failed")
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "BIDNET_BASELINE_COMPLETE": result.get("BIDNET_BASELINE_COMPLETE"),
                    "BIDNET_INCREMENTAL_PRODUCTION_READY": result.get("BIDNET_INCREMENTAL_PRODUCTION_READY"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "canary": result.get("canary"),
                    "baseline": result.get("baseline"),
                    "performance": result.get("performance"),
                    "answers": result.get("answers"),
                    "runtime_s": result.get("runtime_s"),
                },
            )
        except Exception as exc:
            log.exception("BidNet baseline production job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bidnet-baseline-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_baseline_production",
        "active_running": _active_running_id(),
    }


def start_bidnet_engine_recovery_job(
    *,
    stability_sample: int = 6,
    resume_batch: int = 40,
) -> dict[str, Any]:
    """Recover hung BNE job, retest concurrency under thread/browser isolation, resume baseline."""
    job_id = f"BNR-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_engine_recovery",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "stability_sample": int(stability_sample),
            "resume_batch": int(resume_batch),
            "hung_job": "BNE-7fc7c8ba0dfa",
            "sam_calls": 0,
            "rerun_discovery": False,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=180)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "RECOVERY"),
                        "pct": int(kwargs.get("pct") or 0),
                        "workers": kwargs.get("logical") or kwargs.get("workers"),
                        "browser_workers": kwargs.get("browsers") or kwargs.get("browser_workers"),
                        "completed": kwargs.get("completed"),
                        "remaining": kwargs.get("remaining"),
                        "checkpoint_at": kwargs.get("checkpoint_at"),
                    },
                )

            from bidnet_engine.recovery import run_bidnet_engine_recovery

            result = run_bidnet_engine_recovery(
                stability_sample=int(stability_sample),
                resume_batch=int(resume_batch),
                on_progress=_progress,
            )
            try:
                from m3_data_root import data_path

                path = data_path("m3_bidnet_engine_recovery_v1_last_report.json")
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    saved["job_id"] = job_id
                    path.write_text(json.dumps(saved, indent=2, default=str), encoding="utf-8")
            except Exception:
                log.exception("BidNet recovery report patch failed")
            fin = result.get("final") or {}
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_ENGINE_RECOVERY_PASS": result.get("BIDNET_ENGINE_RECOVERY_PASS"),
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "RESOURCE_EXHAUSTION_FIXED": fin.get("RESOURCE_EXHAUSTION_FIXED"),
                    "SAFE_TO_RESUME_BASELINE": fin.get("SAFE_TO_RESUME_BASELINE"),
                    "NEXT_RUN_ALLOWED": fin.get("NEXT_RUN_ALLOWED"),
                    "recovery": result.get("recovery"),
                    "stability": result.get("stability"),
                    "thread_control": result.get("thread_control"),
                    "performance": result.get("performance"),
                    "runtime_s": result.get("runtime_s"),
                },
            )
        except Exception as exc:
            log.exception("BidNet engine recovery job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bidnet-engine-recovery-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_engine_recovery",
        "active_running": _active_running_id(),
    }


def start_bidnet_downstream_deep_job(
    *,
    time_budget_s: int = 5400,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Authenticated detail/package for PRODUCT + MIXED. Requires playwright slot."""
    job_id = f"BDD-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_downstream_deep",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "time_budget_s": int(time_budget_s),
            "max_items": max_items,
            "rerun_discovery": False,
            "sam_calls": 0,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        acquired = _runner_lock.acquire(blocking=True, timeout=120)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:
            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "DEEP"),
                        "pct": int(kwargs.get("pct") or 0),
                        "completed": kwargs.get("completed"),
                        "total": kwargs.get("total"),
                        "pending_remaining": kwargs.get("pending_remaining"),
                        "last_package": kwargs.get("last_package"),
                        "rate_per_min": kwargs.get("rate_per_min"),
                    },
                )

            _progress(phase="AUTH", pct=1)
            from bidnet_downstream.deep import run_bidnet_downstream_deep

            result = run_bidnet_downstream_deep(
                time_budget_s=int(time_budget_s),
                max_items=max_items,
                on_progress=_progress,
            )
            try:
                from m3_data_root import data_path

                report_path = data_path("m3_bidnet_downstream_v1_last_report.json")
                if report_path.exists():
                    saved = json.loads(report_path.read_text(encoding="utf-8"))
                    saved["run_id"] = job_id
                    report_path.write_text(json.dumps(saved, indent=2, default=str), encoding="utf-8")
                    from bidnet_downstream.census import format_downstream_report

                    data_path("m3_bidnet_downstream_v1_last_report.txt").write_text(
                        format_downstream_report(saved),
                        encoding="utf-8",
                    )
            except Exception:
                log.exception("BidNet deep report patch failed")
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_DOWNSTREAM_PASS": result.get("BIDNET_DOWNSTREAM_PASS"),
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "deep": result.get("deep"),
                    "package": result.get("package"),
                    "product_pipeline": result.get("product_pipeline"),
                    "classification": result.get("classification"),
                    "pending_deep_remaining": result.get("pending_deep_remaining"),
                    "runtime_s": result.get("runtime_s"),
                    "safety": result.get("safety"),
                },
            )
        except Exception as exc:
            log.exception("BidNet downstream deep failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bidnet-downstream-deep-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_downstream_deep",
        "active_running": _active_running_id(),
    }


def start_bidnet_downstream_job() -> dict[str, Any]:
    """Classify the frozen valid-open BidNet corpus. Does not rerun discovery or call SAM."""
    job_id = f"BDS-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_downstream",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "valid_open_target": 21976,
            "rerun_discovery": False,
            "sam_calls": 0,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        started = time.time()

        def _progress(**kwargs: Any) -> None:
            _set(
                job_id,
                status="RUNNING",
                progress={
                    "phase": str(kwargs.get("phase") or "CLASSIFY"),
                    "pct": int(kwargs.get("pct") or 0),
                    "completed": kwargs.get("completed"),
                    "total": kwargs.get("total"),
                },
            )

        try:
            _progress(phase="LOAD", pct=1)
            from bidnet_downstream.census import run_bidnet_downstream_census

            result = run_bidnet_downstream_census(on_progress=_progress)
            result["runtime_s"] = round(time.time() - started, 1)
            try:
                from m3_data_root import data_path

                report_path = data_path("m3_bidnet_downstream_v1_last_report.json")
                if report_path.exists():
                    saved = json.loads(report_path.read_text(encoding="utf-8"))
                    saved["runtime_s"] = result["runtime_s"]
                    saved["run_id"] = job_id
                    report_path.write_text(json.dumps(saved, indent=2, default=str), encoding="utf-8")
                    from bidnet_downstream.census import format_downstream_report

                    data_path("m3_bidnet_downstream_v1_last_report.txt").write_text(
                        format_downstream_report(saved),
                        encoding="utf-8",
                    )
            except Exception:
                log.exception("BidNet downstream report runtime patch failed")
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_DOWNSTREAM_PASS": result.get("BIDNET_DOWNSTREAM_PASS"),
                    "PASS_FAIL": result.get("PASS_FAIL"),
                    "NEXT_RUN_ALLOWED": result.get("NEXT_RUN_ALLOWED"),
                    "input_valid_open": result.get("input_valid_open"),
                    "corpus_selection": result.get("corpus_selection"),
                    "corpus_hash": result.get("corpus_hash"),
                    "classification": result.get("classification"),
                    "classification_diff": result.get("classification_diff"),
                    "product_pipeline": result.get("product_pipeline"),
                    "package": result.get("package"),
                    "conservation": result.get("conservation"),
                    "gates": result.get("gates"),
                    "safety": result.get("safety"),
                    "runtime_s": result.get("runtime_s"),
                },
            )
        except Exception as exc:
            log.exception("BidNet downstream census failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"bidnet-downstream-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_downstream",
        "active_running": _active_running_id(),
    }


def start_opengov_discovery_job(
    *,
    max_entities: int | None = 10,
    max_pages: int = 3,
    persist: bool = True,
    mode: str = "cascade",
    allow_browser: bool = False,
) -> dict[str, Any]:
    job_id = f"OGD-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "opengov_discovery",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "max_entities": max_entities,
            "max_pages": max_pages,
            "persist": persist,
            "mode": mode,
            "allow_browser": allow_browser,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 5})
        with _runner_lock:
            _set(job_id, status="RUNNING", progress={"phase": "OPENGOV_CASCADE", "pct": 10})
            try:
                def _progress(**kwargs: Any) -> None:
                    _set(
                        job_id,
                        status="RUNNING",
                        progress={
                            "phase": str(kwargs.get("phase") or "OPENGOV_CASCADE"),
                            "pct": int(kwargs.get("pct") or 10),
                            "entities": kwargs.get("entities"),
                            "retrieved": kwargs.get("retrieved"),
                        },
                    )

                if mode == "legacy":
                    from opengov_discovery import run_opengov_authenticated_discovery

                    result = run_opengov_authenticated_discovery(
                        max_entities=max_entities,
                        max_pages=max_pages,
                        persist=persist,
                        run_id=job_id,
                        use_auth=True,
                        public_first=True,
                        on_progress=_progress,
                    )
                else:
                    from opengov_discovery.cascade import run_opengov_cascade_discovery

                    result = run_opengov_cascade_discovery(
                        max_entities=max_entities,
                        max_pages=max_pages,
                        persist=persist,
                        run_id=job_id,
                        use_auth=True,
                        allow_browser=allow_browser,
                        on_progress=_progress,
                    )
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "auth": (result.get("auth") or {}).get("status")
                        if isinstance(result.get("auth"), dict)
                        else result.get("auth"),
                        "mode": mode,
                        "public_discovery": result.get("public_discovery"),
                        "working_public": result.get("working_public"),
                        "working_auth": result.get("working_auth"),
                        "vendor_global_search": result.get("vendor_global_search"),
                        "entities_attempted": result.get("entities_attempted"),
                        "entities_successful": result.get("entities_successful"),
                        "portal_status_counts": result.get("portal_status_counts"),
                        "route_counts": result.get("route_counts"),
                        "anti_bot_primary_failures": result.get("anti_bot_primary_failures"),
                        "anti_bot_recovered_via_fallback": result.get(
                            "anti_bot_recovered_via_fallback"
                        ),
                        "recovery_blocked": result.get("recovery_blocked"),
                        "blocked_entities": (result.get("blocked_entities") or [])[:40],
                        "pagination_complete_entities": result.get("pagination_complete_entities"),
                        "raw_opportunities": result.get("raw_opportunities"),
                        "unique_records": result.get("unique_records"),
                        "net_new": result.get("net_new"),
                        "canonical_merge": result.get("canonical_merge"),
                        "resolver_telemetry": result.get("resolver_telemetry"),
                        "blocker": result.get("blocker"),
                        "per_entity": {
                            k: {
                                "status": v.get("status"),
                                "working_route": v.get("working_route"),
                                "raw": v.get("raw"),
                                "recovered_via_fallback": v.get("recovered_via_fallback"),
                                "error": v.get("error"),
                            }
                            for k, v in list((result.get("per_entity") or {}).items())[:80]
                        },
                    },
                )
            except Exception as exc:
                log.exception("OpenGov discovery job failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error=type(exc).__name__,
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"opengov-discovery-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "active_running": _active_running_id(),
    }


def start_euna_discovery_job(
    *,
    max_entities: int = 10,
    max_pages: int = 40,
    max_results: int = 5000,
    persist: bool = True,
    mode: str | None = None,
) -> dict[str, Any]:
    # Default: centralized Supplier Network (not agency hub crawl)
    use_central = (mode or "central").lower() not in {"portals", "agency", "hubs"}
    job_id = f"EUD-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "euna_central_discovery" if use_central else "euna_discovery",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "max_entities": max_entities,
            "max_pages": max_pages,
            "max_results": max_results,
            "persist": persist,
            "mode": "central" if use_central else "portals",
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 5})
        with _runner_lock:
            _set(job_id, status="RUNNING", progress={"phase": "EUNA_CENTRAL" if use_central else "EUNA_DISCOVERY", "pct": 10})
            try:
                def _progress(**kwargs: Any) -> None:
                    _set(
                        job_id,
                        status="RUNNING",
                        progress={
                            "phase": str(kwargs.get("phase") or ("EUNA_CENTRAL" if use_central else "EUNA_PUBLIC")),
                            "pct": int(kwargs.get("pct") or 10),
                            "entities": kwargs.get("entities"),
                            "retrieved": kwargs.get("retrieved"),
                            "pages": kwargs.get("pages"),
                            "reported": kwargs.get("reported"),
                        },
                    )

                if use_central:
                    from euna_discovery import run_euna_central_discovery

                    result = run_euna_central_discovery(
                        max_results=max_results,
                        max_pages=max_pages,
                        persist=persist,
                        run_id=job_id,
                        use_auth=True,
                        enrich_portals=False,
                        on_progress=_progress,
                    )
                    auth_dict = result.get("auth") if isinstance(result.get("auth"), dict) else {}
                    _set(
                        job_id,
                        status="COMPLETED",
                        completed_at=_utc(),
                        progress={"phase": "DONE", "pct": 100},
                        result={
                            "mode": "central",
                            "auth": auth_dict.get("status") or result.get("auth"),
                            "failure_reason": result.get("failure_reason")
                            or auth_dict.get("failure_reason"),
                            "account_state": result.get("account_state")
                            or auth_dict.get("account_state"),
                            "flow_detected": result.get("flow_detected")
                            or auth_dict.get("flow_detected"),
                            "auth_host": result.get("auth_host") or auth_dict.get("auth_host"),
                            "visible_error": auth_dict.get("visible_error"),
                            "central_reachable": result.get("central_reachable"),
                            "search_url": result.get("search_url"),
                            "reported_total": result.get("reported_total"),
                            "retrieved_total": result.get("retrieved_total"),
                            "unique_records": result.get("unique_records"),
                            "pages_scanned": result.get("pages_scanned"),
                            "pagination_complete": result.get("pagination_complete"),
                            "account_category_restriction": result.get("account_category_restriction"),
                            "account_entitlement": result.get("account_entitlement"),
                            "net_new": result.get("net_new"),
                            "canonical_merge": result.get("canonical_merge"),
                            "blocker": result.get("blocker"),
                            "page_text_sample": (result.get("page_text_sample") or "")[:400]
                            if result.get("page_text_sample")
                            else None,
                            "nav_clicked": result.get("nav_clicked"),
                            "api_urls_seen": (result.get("api_urls_seen") or [])[:20],
                            "api_fallback": result.get("api_fallback"),
                            "vendor_me_keys": result.get("vendor_me_keys"),
                            "method": result.get("method"),
                        },
                    )
                else:
                    from euna_discovery import run_euna_discovery

                    result = run_euna_discovery(
                        max_entities=max_entities,
                        max_pages=max_pages,
                        persist=persist,
                        run_id=job_id,
                        use_auth=True,
                        on_progress=_progress,
                    )
                    _set(
                        job_id,
                        status="COMPLETED",
                        completed_at=_utc(),
                        progress={"phase": "DONE", "pct": 100},
                        result={
                            "mode": "portals",
                            "auth": (result.get("auth") or {}).get("status")
                            if isinstance(result.get("auth"), dict)
                            else result.get("auth"),
                            "entities_attempted": result.get("entities_attempted"),
                            "entities_successful": result.get("entities_successful"),
                            "working_public": result.get("working_public"),
                            "raw_opportunities": result.get("raw_opportunities"),
                            "unique_records": result.get("unique_records"),
                            "pagination_complete": result.get("pagination_complete"),
                            "portal_status_counts": result.get("portal_status_counts"),
                            "net_new": result.get("net_new"),
                            "canonical_merge": result.get("canonical_merge"),
                        },
                    )
            except Exception:
                log.exception("Euna discovery job failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error="Exception",
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"euna-discovery-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "mode": "central" if use_central else "portals",
        "active_running": _active_running_id(),
    }


def start_euna_api_probe_job() -> dict[str, Any]:
    job_id = f"EUP-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "euna_api_probe",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {},
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 5})
        with _runner_lock:
            _set(job_id, status="RUNNING", progress={"phase": "EUNA_API_PROBE", "pct": 20})
            try:
                from euna_discovery.api_probe import run_euna_api_probe

                result = run_euna_api_probe()
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "probe_version": result.get("probe_version"),
                        "auth": result.get("auth"),
                        "anchor_url": result.get("anchor_url"),
                        "anchor_text_sample": (result.get("anchor_text_sample") or "")[:600],
                        "account_entitlement": result.get("account_entitlement"),
                        "vendors_me_status": result.get("vendors_me_status"),
                        "vendors_me_summary": result.get("vendors_me_summary"),
                        "vendor_flags": result.get("vendor_flags"),
                        "feature_flags_status": result.get("feature_flags_status"),
                        "feature_flags_summary": result.get("feature_flags_summary"),
                        "api_path_probe": result.get("api_path_probe"),
                        "nav_clicks": result.get("nav_clicks"),
                        "xhr_urls": result.get("xhr_urls"),
                        "final_url": result.get("final_url"),
                        "final_text_sample": (result.get("final_text_sample") or "")[:600],
                        "vendors_me_error": result.get("vendors_me_error"),
                        "api_path_probe_error": result.get("api_path_probe_error"),
                    },
                )
            except Exception as exc:
                log.exception("Euna API probe failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error=type(exc).__name__,
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"euna-api-probe-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "active_running": _active_running_id(),
    }


def start_euna_auth_diagnostic_job() -> dict[str, Any]:
    """Stage A — sanitize login diagnostic in background (Playwright)."""
    job_id = f"EUA-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "euna_auth_diagnostic",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {},
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 5})
        with _runner_lock:
            _set(job_id, status="RUNNING", progress={"phase": "EUNA_AUTH_DIAG", "pct": 20})
            try:
                from euna_auth import run_auth_diagnostic

                result = run_auth_diagnostic(persist_screenshot=True)
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "login_success": result.get("login_success"),
                        "failure_reason": result.get("failure_reason"),
                        "account_state": result.get("account_state"),
                        "flow_detected": result.get("flow_detected"),
                        "auth_host": result.get("auth_host"),
                        "final_url": result.get("final_url"),
                        "page_title": result.get("page_title"),
                        "credential_hygiene": result.get("credential_hygiene"),
                        "visible_error": (result.get("auth") or {}).get("visible_error")
                        if isinstance(result.get("auth"), dict)
                        else None,
                        "screenshot_path": result.get("screenshot_path"),
                        "trace": result.get("trace"),
                    },
                )
            except Exception as exc:
                log.exception("Euna auth diagnostic failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error=type(exc).__name__,
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"euna-auth-diag-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "active_running": _active_running_id(),
    }


def start_bidnet_free_package_job(
    *,
    limit: int = 500,
    resume: bool = True,
    force: bool = False,
    stop_if_yield_below: float | None = 0.005,
    universe_mode: bool = False,
) -> dict[str, Any]:
    """Background BidNet free-package chase + economics unblock (no membership)."""
    job_id = f"FPR-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_free_package",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "limit": limit,
            "resume": resume,
            "force": force,
            "stop_if_yield_below": stop_if_yield_below,
            "universe_mode": universe_mode,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "FREE_PACKAGE_BATCH"),
                        "pct": int(kwargs.get("pct") or 5),
                        "retrieved": kwargs.get("retrieved"),
                        "found": kwargs.get("found"),
                        "dead": kwargs.get("dead"),
                        "retryable": kwargs.get("retryable"),
                        "eligible": kwargs.get("eligible"),
                    },
                )

            from bidnet_recovery.free_package_batch import (
                free_package_funnel_report,
                run_free_package_backlog,
            )

            result = run_free_package_backlog(
                limit=int(limit),
                resume=bool(resume),
                force=bool(force),
                persist=True,
                run_id=job_id,
                on_progress=_progress,
                stop_if_yield_below=stop_if_yield_below,
                universe_mode=bool(universe_mode),
            )
            funnel = free_package_funnel_report()
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={"batch": result, "funnel": funnel},
            )
        except Exception as exc:
            log.exception("BidNet free-package job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"free-pkg-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_free_package",
        "active_running": _active_running_id(),
    }


def start_full_production_e2e_job(
    *,
    skip_bidnet: bool = False,
    skip_opengov: bool = False,
    skip_expansion: bool = False,
    bidnet_max_results: int = 25000,
    opengov_max_pages: int = 40,
    recovery_bidnet_limit: int = 400,
    recovery_opengov_limit: int = 200,
    profit_limit: int | None = None,
) -> dict[str, Any]:
    """Background full free-source discovery + universe + recovery + profit report."""
    job_id = f"E2E-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "full_production_e2e",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "skip_bidnet": skip_bidnet,
            "skip_opengov": skip_opengov,
            "skip_expansion": skip_expansion,
            "bidnet_max_results": bidnet_max_results,
            "opengov_max_pages": opengov_max_pages,
            "recovery_bidnet_limit": recovery_bidnet_limit,
            "recovery_opengov_limit": recovery_opengov_limit,
            "profit_limit": profit_limit,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 3})
        with _runner_lock:
            try:

                def _progress(**kwargs: Any) -> None:
                    _set(
                        job_id,
                        status="RUNNING",
                        progress={
                            "phase": str(kwargs.get("phase") or "E2E"),
                            "pct": int(kwargs.get("pct") or 5),
                            "retrieved": kwargs.get("retrieved"),
                            "entities": kwargs.get("entities"),
                            "pages": kwargs.get("pages"),
                            "partition": kwargs.get("partition"),
                        },
                    )

                from full_production_e2e import run_full_production_e2e

                result = run_full_production_e2e(
                    run_id=job_id,
                    on_progress=_progress,
                    skip_bidnet=skip_bidnet,
                    skip_opengov=skip_opengov,
                    skip_expansion=skip_expansion,
                    bidnet_max_results=bidnet_max_results,
                    opengov_max_pages=opengov_max_pages,
                    recovery_bidnet_limit=recovery_bidnet_limit,
                    recovery_opengov_limit=recovery_opengov_limit,
                    profit_limit=profit_limit,
                )
                # Slim result for job store (full report persisted on disk)
                slim = {
                    "full_universe": result.get("full_universe"),
                    "source_counts": result.get("source_counts"),
                    "product_funnel": result.get("product_funnel"),
                    "evidence_funnel": result.get("evidence_funnel"),
                    "economics": result.get("economics"),
                    "profit_buckets": result.get("profit_buckets"),
                    "multi_line": result.get("multi_line"),
                    "source_to_profit": result.get("source_to_profit"),
                    "top_failure_reasons": result.get("top_failure_reasons"),
                    "top_owner_candidates": (result.get("top_owner_candidates") or [])[:15],
                    "plumbing_regression": result.get("plumbing_regression"),
                    "universe_pass": result.get("universe_pass"),
                    "recovery": result.get("recovery"),
                    "started_at": result.get("started_at"),
                    "completed_at": result.get("completed_at"),
                }
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result=slim,
                )
            except Exception as exc:
                log.exception("Full production E2E job failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error=type(exc).__name__,
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"full-e2e-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "full_production_e2e",
        "active_running": _active_running_id(),
    }


def start_full_funnel_sweep_job(
    *,
    skip_bidnet: bool = False,
    skip_opengov: bool = False,
    skip_expansion: bool = False,
    skip_discovery: bool = False,
    bidnet_max_results: int = 25000,
    opengov_max_pages: int = 40,
    free_package_batch_size: int = 400,
    free_package_max_batches: int = 80,
    opengov_recovery_limit: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Background full-universe sweep through END_OF_FUNNEL_READY."""
    job_id = f"FFS-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "full_funnel_sweep",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "skip_bidnet": skip_bidnet,
            "skip_opengov": skip_opengov,
            "skip_expansion": skip_expansion,
            "skip_discovery": skip_discovery,
            "bidnet_max_results": bidnet_max_results,
            "opengov_max_pages": opengov_max_pages,
            "free_package_batch_size": free_package_batch_size,
            "free_package_max_batches": free_package_max_batches,
            "opengov_recovery_limit": opengov_recovery_limit,
            "resume": resume,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "WAITING_SLOT", "pct": 3})
        with _runner_lock:
            try:

                def _progress(**kwargs: Any) -> None:
                    _set(
                        job_id,
                        status="RUNNING",
                        progress={
                            "phase": str(kwargs.get("phase") or "FFS"),
                            "pct": int(kwargs.get("pct") or 5),
                            "retrieved": kwargs.get("retrieved"),
                            "found": kwargs.get("found"),
                            "entities": kwargs.get("entities"),
                            "batch": kwargs.get("batch"),
                            "remaining": kwargs.get("remaining"),
                            "partition": kwargs.get("partition"),
                            "pages": kwargs.get("pages"),
                        },
                    )

                from full_funnel_sweep import format_owner_summary, run_full_funnel_sweep

                result = run_full_funnel_sweep(
                    run_id=job_id,
                    on_progress=_progress,
                    skip_bidnet=bool(skip_bidnet),
                    skip_opengov=bool(skip_opengov),
                    skip_expansion=bool(skip_expansion),
                    skip_discovery=bool(skip_discovery),
                    bidnet_max_results=int(bidnet_max_results),
                    opengov_max_pages=int(opengov_max_pages),
                    free_package_batch_size=int(free_package_batch_size),
                    free_package_max_batches=int(free_package_max_batches),
                    opengov_recovery_limit=opengov_recovery_limit,
                    resume=bool(resume),
                )
                summary = format_owner_summary(result)
                _set(
                    job_id,
                    status="COMPLETED",
                    completed_at=_utc(),
                    progress={"phase": "DONE", "pct": 100},
                    result={
                        "build_target": result.get("build_target"),
                        "full_universe": result.get("full_universe"),
                        "product_funnel": result.get("product_funnel"),
                        "package_funnel": result.get("package_funnel"),
                        "identity_funnel": result.get("identity_funnel"),
                        "evidence_funnel": result.get("evidence_funnel"),
                        "economics_funnel": result.get("economics_funnel"),
                        "profit_buckets": result.get("profit_buckets"),
                        "source_to_end": result.get("source_to_end"),
                        "drop_off": result.get("drop_off"),
                        "top_owner_candidates": result.get("top_owner_candidates"),
                        "questions": result.get("questions"),
                        "owner_summary": summary,
                        "multi_line": result.get("multi_line"),
                    },
                )
            except Exception as exc:
                log.exception("Full funnel sweep failed")
                _set(
                    job_id,
                    status="FAILED",
                    completed_at=_utc(),
                    error=f"{type(exc).__name__}: {exc}"[:400],
                    progress={"phase": "FAILED", "pct": 100},
                )

    threading.Thread(target=_run, name=f"full-ffs-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "full_funnel_sweep",
        "active_running": _active_running_id(),
    }


def start_opengov_public_docs_job(
    *,
    limit: int = 20,
    resume: bool = True,
    download: bool = True,
    max_entities: int | None = None,
    handoff_line_items: bool = True,
) -> dict[str, Any]:
    """Background OpenGov public document recovery (package-access phase)."""
    job_id = f"OGDOC-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "opengov_public_docs",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "limit": limit,
            "resume": resume,
            "download": download,
            "max_entities": max_entities,
            "handoff_line_items": handoff_line_items,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "OPENGOV_PUBLIC_DOCS"),
                        "pct": int(kwargs.get("pct") or 5),
                        "retrieved": kwargs.get("retrieved"),
                        "found": kwargs.get("found"),
                        "docs": kwargs.get("docs"),
                    },
                )

            from opengov_recovery.public_docs_batch import run_opengov_public_docs_stage

            result = run_opengov_public_docs_stage(
                limit=int(limit),
                resume=bool(resume),
                download=bool(download),
                max_entities=max_entities,
                run_id=job_id,
                on_progress=_progress,
                handoff_line_items=bool(handoff_line_items),
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("OpenGov public docs job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"ogdoc-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "opengov_public_docs",
        "active_running": _active_running_id(),
    }


def start_official_source_job(
    *,
    limit: int = 500,
    resume: bool = True,
    download: bool = True,
    handoff_line_items: bool = True,
) -> dict[str, Any]:
    """Background BidNet official-source resolution + free package recovery."""
    job_id = f"OSR-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "official_source",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "limit": limit,
            "resume": resume,
            "download": download,
            "handoff_line_items": handoff_line_items,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "OFFICIAL_SOURCE_BATCH"),
                        "pct": int(kwargs.get("pct") or 5),
                        "retrieved": kwargs.get("retrieved"),
                        "resolved": kwargs.get("resolved"),
                        "found": kwargs.get("found"),
                    },
                )

            from official_source.batch import run_official_source_batch

            result = run_official_source_batch(
                limit=int(limit),
                resume=bool(resume),
                download=bool(download),
                run_id=job_id,
                on_progress=_progress,
                handoff_line_items=bool(handoff_line_items),
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("Official source job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"osr-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "official_source",
        "active_running": _active_running_id(),
    }


def start_scale_evidence_profit_job(
    *,
    mine_buyers: int = 50,
    resume: bool = True,
    identity_limit: int | None = None,
) -> dict[str, Any]:
    """Background scale both-sides → basket economics → lender pipeline."""
    job_id = f"SEP-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "scale_evidence_profit",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "mine_buyers": mine_buyers,
            "resume": resume,
            "identity_limit": identity_limit,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "SCALE"),
                        "pct": int(kwargs.get("pct") or 5),
                        "attempted": kwargs.get("attempted"),
                        "both": kwargs.get("both"),
                        "gov": kwargs.get("gov"),
                        "cost": kwargs.get("cost"),
                        "lender": kwargs.get("lender"),
                        "near": kwargs.get("near"),
                        "buyer": kwargs.get("buyer"),
                    },
                )

            from scale_evidence_profit.batch import run_scale_evidence_to_profit

            result = run_scale_evidence_to_profit(
                mine_buyers=int(mine_buyers),
                resume=bool(resume),
                run_id=job_id,
                on_progress=_progress,
                identity_limit=identity_limit,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("Scale evidence profit job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"sep-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "scale_evidence_profit",
        "active_running": _active_running_id(),
    }


def start_eligibility_and_recovery_job(
    *,
    max_identities: int | None = 400,
    max_opportunities: int | None = 80,
    resume: bool = True,
    allow_live_price: bool = True,
) -> dict[str, Any]:
    """Background eligibility gate + evidence recovery sweep."""
    job_id = f"EAR-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "eligibility_and_recovery",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "max_identities": max_identities,
            "max_opportunities": max_opportunities,
            "resume": resume,
            "allow_live_price": allow_live_price,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "EAR"),
                        "pct": int(kwargs.get("pct") or 5),
                        "attempted": kwargs.get("attempted"),
                        "both": kwargs.get("both"),
                        "opp": kwargs.get("opp"),
                    },
                )

            from eligibility_and_recovery.sweep import run_eligibility_and_recovery_sweep

            result = run_eligibility_and_recovery_sweep(
                on_progress=_progress,
                resume=bool(resume),
                max_identities=max_identities,
                max_opportunities=max_opportunities,
                allow_live_price=bool(allow_live_price),
                run_id=job_id,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "run_id": result.get("run_id"),
                    "eligibility": result.get("ELIGIBILITY"),
                    "funnel": result.get("DISTINCT_OPPORTUNITY_FUNNEL"),
                    "answers": result.get("MOST_IMPORTANT_ANSWERS"),
                    "conservation": result.get("CONSERVATION"),
                },
            )
        except Exception as exc:
            log.exception("Eligibility and recovery job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"ear-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "eligibility_and_recovery",
        "active_running": _active_running_id(),
    }


def start_public_price_search_job(
    *,
    stage_limit: int = 25,
    grades: tuple[str, ...] = ("A",),
    go_metro_priority: bool = True,
    resume: bool = False,
) -> dict[str, Any]:
    """Background human-like public price search + go-metro economics handoff."""
    job_id = f"PPS-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "public_price_search",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "stage_limit": stage_limit,
            "grades": list(grades),
            "go_metro_priority": go_metro_priority,
            "resume": resume,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "PRICE_SEARCH"),
                        "pct": int(kwargs.get("pct") or 5),
                        "key": kwargs.get("key"),
                        "found": kwargs.get("found"),
                    },
                )

            from public_price_search.batch import run_staged_price_search

            result = run_staged_price_search(
                stage_limit=int(stage_limit),
                grades=tuple(grades),
                go_metro_priority=bool(go_metro_priority),
                resume=bool(resume),
                run_id=job_id,
                on_progress=_progress,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("Public price search job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"pps-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "public_price_search",
        "active_running": _active_running_id(),
    }


def start_evidence_breakthrough_job(
    *,
    stage_limit: int = 50,
    resume: bool = True,
    skip_acquisition: bool = False,
) -> dict[str, Any]:
    """Background gov-value + public acquisition-cost evidence breakthrough."""
    job_id = f"EV-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "evidence_breakthrough",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "stage_limit": stage_limit,
            "resume": resume,
            "skip_acquisition": skip_acquisition,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "EVIDENCE"),
                        "pct": int(kwargs.get("pct") or 5),
                        "attempted": kwargs.get("attempted"),
                        "gov": kwargs.get("gov"),
                        "cost": kwargs.get("cost"),
                        "both": kwargs.get("both"),
                    },
                )

            from evidence_breakthrough.batch import run_evidence_breakthrough_stage

            result = run_evidence_breakthrough_stage(
                stage_limit=int(stage_limit),
                resume=bool(resume),
                run_id=job_id,
                on_progress=_progress,
                skip_acquisition=bool(skip_acquisition),
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("Evidence breakthrough job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"ev-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "evidence_breakthrough",
        "active_running": _active_running_id(),
    }


def start_product_identity_job(
    *,
    line_limit: int = 500,
    resume: bool = True,
    handoff: bool = True,
    max_packages: int | None = None,
) -> dict[str, Any]:
    """Background product-identity breakthrough on OpenGov packages."""
    job_id = f"PI-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "product_identity",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {
            "line_limit": line_limit,
            "resume": resume,
            "handoff": handoff,
            "max_packages": max_packages,
        },
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        _set(job_id, status="RUNNING", progress={"phase": "STARTING", "pct": 2})
        try:

            def _progress(**kwargs: Any) -> None:
                _set(
                    job_id,
                    status="RUNNING",
                    progress={
                        "phase": str(kwargs.get("phase") or "PRODUCT_IDENTITY"),
                        "pct": int(kwargs.get("pct") or 5),
                        "packages": kwargs.get("packages"),
                        "usable": kwargs.get("usable"),
                        "raw": kwargs.get("raw"),
                    },
                )

            from product_identity.batch import run_product_identity_stage

            result = run_product_identity_stage(
                line_limit=int(line_limit),
                resume=bool(resume),
                handoff=bool(handoff),
                run_id=job_id,
                on_progress=_progress,
                max_packages=max_packages,
            )
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result=result,
            )
        except Exception as exc:
            log.exception("Product identity job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                progress={"phase": "FAILED", "pct": 100},
            )

    threading.Thread(target=_run, name=f"pi-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "product_identity",
        "active_running": _active_running_id(),
    }


def start_bidnet_full_production_job(
    *,
    fresh: bool = True,
    full_discovery: bool = False,
) -> dict[str, Any]:
    """Background BidNet full production: auth-required 192 acceptance (+ optional full discovery)."""
    job_id = f"BNFP-{uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "kind": "bidnet_full_production",
        "status": "QUEUED",
        "started_at": _utc(),
        "updated_at": _utc(),
        "completed_at": None,
        "params": {"fresh": fresh, "full_discovery": full_discovery, "skip_live_detail": False},
        "progress": {"phase": "QUEUED", "pct": 0},
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _persist(job)

    def _run() -> None:
        # Wait briefly for Playwright slot instead of failing immediately.
        acquired = _runner_lock.acquire(blocking=True, timeout=120)
        if not acquired:
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error="another_playwright_job_running",
                progress={"phase": "FAILED", "pct": 100},
            )
            return
        try:
            _set(job_id, status="RUNNING", progress={"phase": "AUTH", "pct": 2})
            from bidnet_full_production.sweep import run_bidnet_full_production_v1
            from m3_data_root import data_path
            import json as _json

            def _tick() -> None:
                p = data_path("m3_bidnet_full_production_v1_progress.json")
                if p.exists():
                    try:
                        prog = _json.loads(p.read_text(encoding="utf-8"))
                        _set(
                            job_id,
                            status="RUNNING",
                            progress={
                                "phase": str(prog.get("stage") or "RUNNING"),
                                "pct": int(prog.get("progress_pct") or 5),
                                "detail": prog.get("detail"),
                            },
                        )
                    except Exception:
                        pass

            # Heartbeat thread
            stop = threading.Event()

            def _hb() -> None:
                while not stop.wait(15):
                    _tick()

            hb = threading.Thread(target=_hb, name=f"bnfp-hb-{job_id}", daemon=True)
            hb.start()
            try:
                result = run_bidnet_full_production_v1(
                    resume=not bool(fresh),
                    run_full_discovery=bool(full_discovery),
                    skip_live_detail=False,
                )
            finally:
                stop.set()
            _set(
                job_id,
                status="COMPLETED",
                completed_at=_utc(),
                progress={"phase": "DONE", "pct": 100},
                result={
                    "BIDNET_PRODUCTION_PASS": result.get("BIDNET_PRODUCTION_PASS"),
                    "acceptance_192": result.get("acceptance_192"),
                    "session": result.get("session"),
                    "details": result.get("details"),
                    "package_access": result.get("package_access"),
                    "discovery": result.get("discovery"),
                    "answers": result.get("answers"),
                },
            )
        except Exception as exc:
            log.exception("BidNet full production job failed")
            _set(
                job_id,
                status="FAILED",
                completed_at=_utc(),
                error=f"{type(exc).__name__}: {exc}"[:500],
                progress={"phase": "FAILED", "pct": 100},
            )
        finally:
            _runner_lock.release()

    threading.Thread(target=_run, name=f"bnfp-{job_id}", daemon=True).start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "QUEUED",
        "kind": "bidnet_full_production",
        "active_running": _active_running_id(),
    }
