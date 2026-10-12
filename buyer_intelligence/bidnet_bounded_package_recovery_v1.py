"""Bounded BidNet package recovery for the fixed 36 AUTH_REQUIRED cohort.

LONG_RUNNING_JOB_EXECUTION_STANDARD:
- per-opportunity hard timeout (default 30s, max 45s)
- checkpoint after every opportunity
- heartbeat <=30s
- resume without redoing SUCCESS
- force-reset preserves checkpoints
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from buyer_intelligence.bidnet_auth_recovery_package_closure_v1 import (
    EXTERNAL_CREDENTIALS,
    attempt_authentication,
    compute_full100_progression,
    freeze_auth_required_36,
    recover_one_authenticated,
    _data,
    _load_json,
    _write_json,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_jobs.store import JobLeaseError, JobStore

log = logging.getLogger("govtracker.bidnet_bounded_recovery")

BUILD = "20261009-m3-bidnet-recovery-bounded-checkpoint-resume-v1"
FAMILY = "bidnet_package_recovery"
COHORT_KIND = "BIDNET_AUTH_REQUIRED_36"
ITEM_TIMEOUT_SEC = 30.0
ITEM_TIMEOUT_MAX_SEC = 45.0
STALE_SECONDS = float(LONG_RUNNING_JOB_EXECUTION_STANDARD["STALE_RUNNING_SECONDS"])
HEARTBEAT_EVERY = 15.0
MAX_DOCS_FIRST_PASS = 5
REPORT_FILE = "m3_bidnet_bounded_package_recovery_v1_report.json"
RESULTS_FILE = "m3_bidnet_bounded_package_recovery_v1_results.json"
TODAY_FILE = "m3_bidnet_bounded_package_recovery_today_v1.json"

TERMINAL_SUCCESS = {"SUCCESS", "SKIPPED_ALREADY_COMPLETE"}
TERMINAL_DONE = {
    "SUCCESS",
    "PARTIAL",
    "TIMEOUT",
    "AUTH_FAILED",
    "NO_PACKAGE",
    "FETCH_FAILED",
    "PARSE_FAILED",
    "SKIPPED_ALREADY_COMPLETE",
}


def _store() -> JobStore:
    return JobStore(FAMILY)


def _classify_result(rec: dict[str, Any]) -> str:
    pkg = str(rec.get("PACKAGE_STATUS") or "")
    doc_class = str(rec.get("DOCUMENT_CLASSIFICATION") or "")
    if pkg == EXTERNAL_CREDENTIALS or rec.get("BLOCKER") == EXTERNAL_CREDENTIALS or doc_class == "ACCOUNT_NOT_ENTITLED":
        return "AUTH_FAILED"
    downloaded = int(rec.get("DOC_DOWNLOADED_COUNT") or 0)
    real_lines = int(rec.get("LINE_COUNT") or 0)
    # SUCCESS requires recovered docs and/or real (non-title) lines — not detail-only
    if downloaded > 0 or (real_lines > 0 and rec.get("NEW_SOURCE_DEPTH") in {"RICH", "USABLE"}):
        return "SUCCESS"
    if pkg in {"PACKAGE_RECOVERED_RICH", "PACKAGE_RECOVERED_USABLE", "PACKAGE_ACQUIRED_OFFICIAL_SOURCE"} and (
        downloaded > 0 or real_lines > 0
    ):
        return "SUCCESS"
    if doc_class == "NO_DOCUMENTS_PUBLISHED" and not real_lines:
        return "NO_PACKAGE"
    if pkg in {"PACKAGE_PARTIAL", "PACKAGE_REGISTRATION_REQUIRED", "EXTERNAL_ENTITLEMENT_REQUIRED"}:
        return "PARTIAL"
    if str(rec.get("BLOCKER") or "").startswith("AUTH_DETAIL"):
        return "FETCH_FAILED"
    if rec.get("AUTHENTICATED_SUCCESS"):
        # Authenticated detail without documents/real lines = PARTIAL, not SUCCESS
        return "PARTIAL"
    return "FETCH_FAILED"


def _timeout_record(row: dict[str, Any], *, started_at: str, duration: float) -> dict[str, Any]:
    return {
        "OPPORTUNITY_ID": row.get("OPPORTUNITY_ID"),
        "SOURCE": "BidNet",
        "BUYER": row.get("BUYER"),
        "TITLE": row.get("TITLE"),
        "PACKAGE_STATUS": "TIMEOUT",
        "FINAL_DISPOSITION": "RESEARCH",
        "BLOCKER": "PER_OPPORTUNITY_TIMEOUT",
        "NEXT_ACTION": "RETRY_BOUNDED",
        "WHY": f"Exceeded per-opportunity wall {ITEM_TIMEOUT_SEC}s",
        "DOC_COUNT": 0,
        "LINE_COUNT": 0,
        "LINES": [],
        "DOCUMENTS": [],
        "IDENTITY_STATE": "RESEARCH",
        "REVENUE_STATE": "CURRENT_REVENUE_UNKNOWN",
        "AUTHENTICATED_SUCCESS": False,
        "RESULT": "TIMEOUT",
        "ERROR_CODE": "PER_OPPORTUNITY_TIMEOUT",
        "STARTED_AT": started_at,
        "FINISHED_AT": datetime.now(timezone.utc).isoformat(),
        "DURATION_SECONDS": round(duration, 3),
        "BUILD": BUILD,
    }


def _run_one_with_timeout(
    fn: Callable[[], dict[str, Any]],
    *,
    timeout_sec: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run fn in a worker thread; on timeout return (None, 'TIMEOUT')."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout_sec), None
        except concurrent.futures.TimeoutError:
            return None, "TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            return None, type(exc).__name__


def stop_stalled_opaque_job(*, cancel_bounded_if_stale: bool = True) -> dict[str, Any]:
    """Cancel legacy opaque RUNNING marker; release stale bounded leases only."""
    from m3_data_root import data_path

    out: dict[str, Any] = {
        "legacy_job": None,
        "bounded_job": None,
        "defect": None,
    }
    legacy = data_path("m3_bidnet_auth_recovery_package_closure_v1_job.json")
    if legacy.exists():
        try:
            cur = _load_json(legacy) or {}
        except Exception:
            cur = {}
        out["legacy_job"] = {
            "JOB_ID": "legacy-opaque-full36",
            "STARTED_AT": cur.get("started_at"),
            "LAST_PROGRESS_AT": cur.get("heartbeat_at") or cur.get("started_at"),
            "CURRENT_ITEM": None,
            "COMPLETED_COUNT": 0,
            "SUCCESS_COUNT": 0,
            "TIMEOUT_COUNT": 0,
            "FAIL_COUNT": 0,
            "STATUS_BEFORE": cur.get("status"),
            "DEFECT": "NO_PER_ITEM_CHECKPOINT" if cur.get("status") == "RUNNING" else None,
        }
        if cur.get("status") == "RUNNING":
            cur["status"] = "CANCELLED"
            cur["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            cur["reason"] = "REPLACED_BY_BOUNDED_CHECKPOINT_RUNNER"
            _write_json(legacy, cur)
            out["legacy_job"]["STATUS_AFTER"] = "CANCELLED"
        if out["legacy_job"]["DEFECT"]:
            out["defect"] = "NO_PER_ITEM_CHECKPOINT_ON_OPAQUE_JOB"
    store = _store()
    active = store.find_active_job(stale_seconds=STALE_SECONDS)
    if active:
        out["bounded_job"] = store.progress_payload(str(active["JOB_ID"]))
        if cancel_bounded_if_stale and active.get("STATUS") in {"STALE_RELEASED"}:
            store.force_reset_running_lock(str(active["JOB_ID"]))
            out["bounded_job"] = store.progress_payload(str(active["JOB_ID"]))
    return out


def start_or_resume_job(*, force: bool = False, hang_inject_oid: str | None = None) -> dict[str, Any]:
    """Enqueue/resume bounded 36 recovery. Returns quickly with JOB_ID."""
    stop_info = stop_stalled_opaque_job(cancel_bounded_if_stale=True)
    store = _store()
    cohort = freeze_auth_required_36()
    rows = list(cohort.get("rows") or [])
    ids = [str(r["OPPORTUNITY_ID"]) for r in rows]
    if len(ids) != 36:
        return {"status": "ERROR", "error": f"COHORT_COUNT_{len(ids)}_EXPECTED_36", "stop": stop_info}

    active = store.find_active_job(stale_seconds=STALE_SECONDS)
    if (
        active
        and active.get("STATUS") in {"RUNNING", "CREATED"}
        and list(active.get("OPPORTUNITY_IDS") or []) == ids
        and not force
    ):
        return {
            "status": "ALREADY_RUNNING",
            "JOB_ID": active.get("JOB_ID"),
            "progress": store.progress_payload(str(active["JOB_ID"])),
            "stop": stop_info,
        }

    if active and active.get("STATUS") in {"INTERRUPTED", "STALE_RELEASED", "CANCELLED"} and not force:
        # Resume same job id if same cohort
        existing_ids = list(active.get("OPPORTUNITY_IDS") or [])
        if existing_ids == ids:
            job_id = str(active["JOB_ID"])
            _spawn_worker(job_id, hang_inject_oid=hang_inject_oid)
            return {"status": "RESUMED", "JOB_ID": job_id, "progress": store.progress_payload(job_id), "stop": stop_info}

    if force and active and active.get("JOB_ID"):
        try:
            store.force_reset_running_lock(str(active["JOB_ID"]))
        except Exception:
            pass

    job = store.create_job(
        total=36,
        opportunity_ids=ids,
        meta={
            "BUILD": BUILD,
            "COHORT_KIND": COHORT_KIND,
            "COHORT_CHECKSUM": cohort.get("checksum"),
            "ITEM_TIMEOUT_SEC": ITEM_TIMEOUT_SEC,
            "ITEM_TIMEOUT_MAX_SEC": ITEM_TIMEOUT_MAX_SEC,
            "MAX_DOCS_FIRST_PASS": MAX_DOCS_FIRST_PASS,
            "STANDARD": "LONG_RUNNING_JOB_EXECUTION_STANDARD",
        },
    )
    job_id = str(job["JOB_ID"])
    # Mark RUNNING before spawn so duplicate starts see the active job immediately.
    job["STATUS"] = "RUNNING"
    job["STARTED_AT"] = job.get("STARTED_AT") or datetime.now(timezone.utc).isoformat()
    job["HEARTBEAT_AT"] = datetime.now(timezone.utc).isoformat()
    store.save_job(job)
    from m3_jobs.store import _atomic_write

    _atomic_write(store.active_pointer_path(), {"JOB_ID": job_id, "UPDATED_AT": job["HEARTBEAT_AT"]})
    # Seed STARTED placeholders only when missing
    for idx, oid in enumerate(ids):
        existing = store.load_item(job_id, oid)
        if existing and existing.get("RESULT") in TERMINAL_DONE:
            continue
        if not existing:
            store.write_item_checkpoint(
                job_id,
                {
                    "OPPORTUNITY_ID": oid,
                    "INDEX": idx,
                    "RESULT": None,
                    "STATUS": "PENDING",
                    "TITLE": next((r.get("TITLE") for r in rows if str(r.get("OPPORTUNITY_ID")) == oid), None),
                },
            )
    _spawn_worker(job_id, hang_inject_oid=hang_inject_oid)
    return {"status": "STARTED", "JOB_ID": job_id, "progress": store.progress_payload(job_id), "stop": stop_info}


_worker_threads: dict[str, threading.Thread] = {}


def _spawn_worker(job_id: str, *, hang_inject_oid: str | None = None) -> None:
    t = threading.Thread(
        target=run_job_worker,
        kwargs={"job_id": job_id, "hang_inject_oid": hang_inject_oid},
        name=f"bidnet-bounded-{job_id[-8:]}",
        daemon=True,
    )
    _worker_threads[job_id] = t
    t.start()


def run_job_worker(job_id: str, *, hang_inject_oid: str | None = None) -> dict[str, Any]:
    store = _store()
    worker_id = f"worker-{uuid.uuid4().hex[:10]}"
    try:
        store.acquire_lease(job_id, worker_id, stale_seconds=STALE_SECONDS, force=False)
    except JobLeaseError as exc:
        return {"status": "LEASE_DENIED", "error": str(exc), "JOB_ID": job_id}

    cohort = freeze_auth_required_36()
    rows = list(cohort.get("rows") or [])
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in rows}
    job = store.load_job(job_id) or {}
    ids = list(job.get("OPPORTUNITY_IDS") or [str(r["OPPORTUNITY_ID"]) for r in rows])

    auth_attempt = attempt_authentication()
    client = auth_attempt.pop("client", None)
    auth_ok = bool(auth_attempt.get("authenticated"))

    l23_store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}
    price_cache = _load_json(_data("m3_commercial_acquisition_cache_v1.json")) or {}
    quotes = _load_json(_data("m3_processed_real_quotes_v1.json")) or {}
    now = datetime.now(timezone.utc)

    last_hb = 0.0
    results: list[dict[str, Any]] = []

    try:
        for idx, oid in enumerate(ids):
            # Heartbeat
            if time.perf_counter() - last_hb >= HEARTBEAT_EVERY:
                store.heartbeat(job_id, worker_id=worker_id, current_index=idx, current_opportunity_id=oid)
                last_hb = time.perf_counter()

            existing = store.load_item(job_id, oid) or {}
            # Preserve completed successes — never reprocess.
            if existing.get("RESULT") in TERMINAL_SUCCESS:
                results.append(existing.get("RECORD") or existing)
                store.recompute_counts(job_id)
                continue
            # Preserve non-retryable terminals.
            if existing.get("RESULT") in {"AUTH_FAILED", "NO_PACKAGE", "PARSE_FAILED"}:
                results.append(existing.get("RECORD") or existing)
                store.recompute_counts(job_id)
                continue

            row = by_id.get(oid) or {"OPPORTUNITY_ID": oid}
            started_at = datetime.now(timezone.utc).isoformat()
            store.write_item_checkpoint(
                job_id,
                {
                    "OPPORTUNITY_ID": oid,
                    "INDEX": idx,
                    "STATUS": "STARTED",
                    "RESULT": None,
                    "STARTED_AT": started_at,
                    "TITLE": row.get("TITLE"),
                    "CURRENT_DOC": None,
                },
            )
            store.heartbeat(job_id, worker_id=worker_id, current_index=idx, current_opportunity_id=oid)
            last_hb = time.perf_counter()

            if hang_inject_oid and oid == hang_inject_oid:
                def _hang() -> dict[str, Any]:
                    time.sleep(ITEM_TIMEOUT_MAX_SEC + 5)
                    return {}

                rec, err = _run_one_with_timeout(_hang, timeout_sec=ITEM_TIMEOUT_SEC)
                if err == "TIMEOUT":
                    rec = _timeout_record(row, started_at=started_at, duration=ITEM_TIMEOUT_SEC)
                else:
                    rec = _timeout_record(row, started_at=started_at, duration=ITEM_TIMEOUT_SEC)
            elif not auth_ok:
                rec = recover_one_authenticated(
                    row,
                    client=None,
                    l23=dict(l23_store.get(oid) or {}),
                    auth_status=auth_attempt,
                    price_cache=price_cache,
                    quotes=quotes,
                    now=now,
                )
                rec["RESULT"] = "AUTH_FAILED"
                rec["STARTED_AT"] = started_at
                rec["FINISHED_AT"] = datetime.now(timezone.utc).isoformat()
                rec["DURATION_SECONDS"] = float(rec.get("ELAPSED_SEC") or 0)
            else:
                l23 = dict(l23_store.get(oid) or {})
                if not l23:
                    l23 = {
                        "canonical_opportunity_id": oid,
                        "title": row.get("TITLE"),
                        "buyer": row.get("BUYER"),
                        "deadline": row.get("DEADLINE"),
                        "authoritative_url": row.get("PORTAL"),
                    }

                def _work() -> dict[str, Any]:
                    return recover_one_authenticated(
                        row,
                        client=client,
                        l23=l23,
                        auth_status=auth_attempt,
                        price_cache=price_cache,
                        quotes=quotes,
                        now=now,
                    )

                t0 = time.perf_counter()
                rec, err = _run_one_with_timeout(_work, timeout_sec=ITEM_TIMEOUT_SEC)
                dur = time.perf_counter() - t0
                if err == "TIMEOUT" or rec is None:
                    rec = _timeout_record(row, started_at=started_at, duration=min(dur, ITEM_TIMEOUT_MAX_SEC))
                else:
                    result = _classify_result(rec)
                    # Cap docs in first pass reporting
                    docs = list(rec.get("DOCUMENTS") or [])
                    if len(docs) > MAX_DOCS_FIRST_PASS:
                        rec["DOCUMENTS_DEFERRED"] = len(docs) - MAX_DOCS_FIRST_PASS
                        rec["DOCUMENTS"] = docs[:MAX_DOCS_FIRST_PASS]
                        rec["DOC_COUNT"] = len(rec["DOCUMENTS"])
                    rec["RESULT"] = result
                    rec["STARTED_AT"] = started_at
                    rec["FINISHED_AT"] = datetime.now(timezone.utc).isoformat()
                    rec["DURATION_SECONDS"] = round(float(rec.get("ELAPSED_SEC") or dur), 3)
                    if rec["DURATION_SECONDS"] > ITEM_TIMEOUT_MAX_SEC:
                        # Should not happen if timeout works; clamp classification
                        rec["RESULT"] = "TIMEOUT"
                        rec["ERROR_CODE"] = "EXCEEDED_MAX_AFTER_RETURN"
                        rec["BLOCKER"] = "PER_OPPORTUNITY_TIMEOUT"

            assert rec is not None
            result = str(rec.get("RESULT") or _classify_result(rec))
            checkpoint = {
                "OPPORTUNITY_ID": oid,
                "INDEX": idx,
                "STATUS": result,
                "RESULT": result,
                "STARTED_AT": rec.get("STARTED_AT") or started_at,
                "FINISHED_AT": rec.get("FINISHED_AT") or datetime.now(timezone.utc).isoformat(),
                "DURATION_SECONDS": rec.get("DURATION_SECONDS"),
                "DOC_COUNT": rec.get("DOC_COUNT") or 0,
                "LINE_COUNT": rec.get("LINE_COUNT") or 0,
                "PACKAGE_STATE": rec.get("PACKAGE_STATUS") or rec.get("PROCESS_PACKAGE_STATE"),
                "ERROR_CODE": rec.get("ERROR_CODE") or rec.get("BLOCKER"),
                "NEXT_ACTION": rec.get("NEXT_ACTION"),
                "TITLE": rec.get("TITLE") or row.get("TITLE"),
                "NEW_SOURCE_DEPTH": rec.get("NEW_SOURCE_DEPTH"),
                "IDENTITY_STATE": rec.get("IDENTITY_STATE"),
                "IDENTITY_QUOTE_READY": rec.get("IDENTITY_QUOTE_READY"),
                "REVENUE_STATE": rec.get("REVENUE_STATE"),
                "FINAL_DISPOSITION": rec.get("FINAL_DISPOSITION"),
                "RECORD": rec,
                "BUILD": BUILD,
            }
            store.write_item_checkpoint(job_id, checkpoint)
            store.recompute_counts(job_id)
            results.append(rec)

            # If auth died mid-run, mark remaining as AUTH_FAILED and stop
            if result == "AUTH_FAILED" and auth_attempt.get("external_blocker") == EXTERNAL_CREDENTIALS:
                for j in range(idx + 1, len(ids)):
                    oid2 = ids[j]
                    prev = store.load_item(job_id, oid2)
                    if prev and prev.get("RESULT") in TERMINAL_DONE:
                        continue
                    row2 = by_id.get(oid2) or {"OPPORTUNITY_ID": oid2}
                    fail = recover_one_authenticated(
                        row2,
                        client=None,
                        l23={},
                        auth_status=auth_attempt,
                        price_cache=price_cache,
                        quotes=quotes,
                        now=now,
                    )
                    fail["RESULT"] = "AUTH_FAILED"
                    fail["STARTED_AT"] = datetime.now(timezone.utc).isoformat()
                    fail["FINISHED_AT"] = fail["STARTED_AT"]
                    fail["DURATION_SECONDS"] = 0
                    store.write_item_checkpoint(
                        job_id,
                        {
                            "OPPORTUNITY_ID": oid2,
                            "INDEX": j,
                            "STATUS": "AUTH_FAILED",
                            "RESULT": "AUTH_FAILED",
                            "STARTED_AT": fail["STARTED_AT"],
                            "FINISHED_AT": fail["FINISHED_AT"],
                            "DURATION_SECONDS": 0,
                            "DOC_COUNT": 0,
                            "LINE_COUNT": 0,
                            "PACKAGE_STATE": EXTERNAL_CREDENTIALS,
                            "ERROR_CODE": EXTERNAL_CREDENTIALS,
                            "NEXT_ACTION": "PROVIDE_BIDNET_CREDENTIALS",
                            "RECORD": fail,
                            "BUILD": BUILD,
                        },
                    )
                    results.append(fail)
                store.recompute_counts(job_id)
                break

        # Assemble final report from disk checkpoints (not in-memory only)
        report = finalize_report_from_disk(job_id, auth_attempt=auth_attempt)
        store.release_lease(job_id, worker_id, final_status="DONE")
        return report
    except Exception as exc:  # noqa: BLE001
        log.exception("bounded recovery worker failed")
        job = store.load_job(job_id) or {}
        job["LAST_ERROR"] = type(exc).__name__
        store.save_job(job)
        store.release_lease(job_id, worker_id, final_status="ERROR")
        return {"status": "ERROR", "JOB_ID": job_id, "error": type(exc).__name__}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def finalize_report_from_disk(job_id: str, *, auth_attempt: dict[str, Any] | None = None) -> dict[str, Any]:
    store = _store()
    job = store.recompute_counts(job_id)
    items = store.list_items(job_id)
    # Preserve cohort order
    ids = list(job.get("OPPORTUNITY_IDS") or [])
    by_oid = {str(i.get("OPPORTUNITY_ID")): i for i in items}
    ordered = [by_oid[i] for i in ids if i in by_oid]
    records = []
    for it in ordered:
        rec = it.get("RECORD") if isinstance(it.get("RECORD"), dict) else it
        records.append(rec)

    durations = [float(i.get("DURATION_SECONDS") or 0) for i in ordered if i.get("DURATION_SECONDS") is not None]
    depth_c: dict[str, int] = {}
    for r in records:
        d = str(r.get("NEW_SOURCE_DEPTH") or "UNKNOWN")
        depth_c[d] = depth_c.get(d, 0) + 1

    progression = compute_full100_progression(records)
    over45 = sum(1 for d in durations if d > ITEM_TIMEOUT_MAX_SEC)

    report = {
        "BUILD": BUILD,
        "JOB_ID": job_id,
        "FINAL_STATUS": "BOUNDED_RECOVERY_COMPLETE_WITH_TIMEOUTS"
        if int(job.get("TIMEOUT") or 0) > 0
        else "BOUNDED_RECOVERY_WORKING",
        "STATUS": job.get("STATUS"),
        "COHORT_36": {"count": len(ids), "preserved": len(ids) == 36},
        "COUNTS": {
            "TOTAL": 36,
            "COMPLETED": job.get("COMPLETED"),
            "SUCCESS": job.get("SUCCESS"),
            "PARTIAL": job.get("PARTIAL"),
            "TIMEOUT": job.get("TIMEOUT"),
            "AUTH_FAILED": job.get("AUTH_FAILED"),
            "NO_PACKAGE": job.get("NO_PACKAGE"),
            "FETCH_FAILED": job.get("FETCH_FAILED"),
            "PARSE_FAILED": job.get("PARSE_FAILED"),
            "SKIPPED_ALREADY_COMPLETE": job.get("SKIPPED_ALREADY_COMPLETE"),
        },
        "DEPTH": depth_c,
        "DOCUMENTS": sum(int(r.get("DOC_COUNT") or 0) for r in records),
        "LINES": sum(int(r.get("LINE_COUNT") or 0) for r in records),
        "IDENTITY_ADEQUATE": sum(1 for r in records if r.get("IDENTITY_STATE") == "ADEQUATE"),
        "QUOTE_READY": sum(1 for r in records if r.get("IDENTITY_QUOTE_READY")),
        "REVENUE_ADEQUATE": sum(
            1 for r in records if r.get("REVENUE_STATE") in {"CURRENT_REVENUE_PROVEN", "CURRENT_REVENUE_PARTIAL"}
        ),
        "TIMING": {
            "longest_seconds": max(durations) if durations else 0,
            "average_seconds": round(sum(durations) / len(durations), 3) if durations else 0,
            "any_over_45s": over45,
        },
        "FULL_100_PROGRESSION": progression,
        "AUTH": {
            k: (auth_attempt or {}).get(k)
            for k in ("authenticated", "session_reused", "status", "external_blocker")
        },
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "FAILURES": [
            {
                "OPPORTUNITY_ID": i.get("OPPORTUNITY_ID"),
                "TITLE": i.get("TITLE"),
                "RESULT": i.get("RESULT"),
                "ERROR_CODE": i.get("ERROR_CODE"),
                "DURATION_SECONDS": i.get("DURATION_SECONDS"),
                "NEXT_ACTION": i.get("NEXT_ACTION"),
            }
            for i in ordered
            if i.get("RESULT") not in {"SUCCESS", "SKIPPED_ALREADY_COMPLETE", "PARTIAL"}
        ],
        "FRESH_SOURCE_FETCHES": "UNKNOWN",
        "CACHE_HITS": "UNKNOWN",
        "PREEXISTING_EVIDENCE_REUSED": "UNKNOWN",
        "NEW_EVIDENCE_CREATED": "UNKNOWN",
        "EXECUTION_MODE": "FRESH_PRODUCTION_EXECUTION",
    }
    _write_json(_data(REPORT_FILE), report)
    _write_json(_data(RESULTS_FILE), {"build": BUILD, "JOB_ID": job_id, "count": len(records), "rows": records})
    _write_json(
        _data(TODAY_FILE),
        {
            "build": BUILD,
            "JOB_ID": job_id,
            "counts": report["COUNTS"],
            "depth": depth_c,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    # Also mirror into legacy report path for UI compatibility
    _write_json(_data("m3_bidnet_auth_recovery_package_closure_v1_report.json"), {**report, "kind": "BOUNDED_V1"})
    _write_json(
        _data("m3_bidnet_auth_recovery_package_closure_v1_results.json"),
        {"build": BUILD, "JOB_ID": job_id, "count": len(records), "rows": records},
    )
    return report


def job_progress(job_id: str | None = None) -> dict[str, Any]:
    store = _store()
    if not job_id:
        active = store.find_active_job(stale_seconds=STALE_SECONDS)
        if not active:
            return {"status": "NO_ACTIVE_JOB"}
        job_id = str(active["JOB_ID"])
    return store.progress_payload(job_id)


def owner_progress_card(job_id: str | None = None) -> dict[str, Any]:
    p = job_progress(job_id)
    if p.get("status") == "NO_ACTIVE_JOB":
        return {"title": "BIDNET PACKAGE RECOVERY", "status": "NO_ACTIVE_JOB"}
    return {
        "title": "BIDNET PACKAGE RECOVERY",
        "JOB_ID": p.get("JOB_ID"),
        "status": p.get("STATUS"),
        "Completed": f"{p.get('COMPLETED')}/{p.get('TOTAL')}",
        "Success": p.get("SUCCESS"),
        "Partial": p.get("PARTIAL"),
        "Timeout": p.get("TIMEOUT"),
        "Failed": p.get("FAILED"),
        "Current": p.get("CURRENT_OPPORTUNITY_ID"),
        "Last progress": p.get("LAST_CHECKPOINT_AT") or p.get("HEARTBEAT_AT"),
    }
