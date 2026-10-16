"""Full-scale production funnel — process ALL LIVE_ELIGIBLE product opportunities.

Continuous, checkpointed, exclusive-lease worker. Reuses CFE process_one
(BidNet bounded recovery → product lines → distributors → RFQ prep → economics).

Not a pilot: no artificial total cap. Internal slices are an engineering
mechanism only — the worker continues automatically until the eligible queue
is exhausted, stop is requested, or a hard budget/auth blocker.

AUTO_SEND remains false. No OpenAI / SAM spend in the default path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_jobs.store import JobLeaseError, JobStore

log = logging.getLogger("govtracker.full_scale_production_funnel_v1")

BUILD = "20261016-m3-full-scale-production-funnel-v4e27"
FAMILY = "full_scale_production_funnel_v1"
MAX_TECHNICAL_RETRIES = 2
# First-pass: no immediate per-opp retry loops. Deferred recovery is second pass.
FIRST_PASS_NO_INLINE_RETRY = True
# Preserve — do not auto-retry
_PRESERVE_RESULTS = {
    "SUCCESS",
    "EXPIRED_EXIT",
    "INELIGIBLE_EXIT",
    "OWNER_ACTION_REQUIRED",
    "SKIPPED_ALREADY_COMPLETE",
}
# Systemic technical failures eligible for bounded retry after repair
_RETRYABLE_BLOCKERS = {
    "ABSTRACT_FETCH_FAILED_OR_WAF",
    "ABSTRACT_PARSE_UNAVAILABLE",
    "DETAIL_FETCH_EMPTY",
    "SOLICITATION_RESOLUTION_FAILED",
    "WAF_OR_CHALLENGE_PAGE",
    "MISSING_AUTHORITATIVE_URL",
    "PER_OPPORTUNITY_TIMEOUT_45S",
    "PER_OPPORTUNITY_TIMEOUT",
    "FETCH_FAILED",
    "INCOMPLETE_LINE_ITEMS",
    "BIDNET_AUTH_REQUIRED",
    "DOCUMENTS_INACCESSIBLE",
    # Retry after group-level registration succeeds (not permanently failed)
    "PURCHASING_GROUP_REGISTRATION_REQUIRED",
    "AWAITING_PURCHASING_GROUP_REGISTRATION",
}
OUT = "_og_commercial_research_pipeline_out"
ARCHIVE_DIR = f"{OUT}/m3_fspf_v1_archived_runs"

FCF_CLASS = f"{OUT}/m3_fcf_v1_classifications.json"
FCF_SCORE = f"{OUT}/m3_fcf_v1_scorecard.json"
QUEUE_FILE = f"{OUT}/m3_fspf_v1_eligible_queue.json"
DEFERRED_QUEUE_FILE = f"{OUT}/m3_fspf_v1_deferred_queue.json"
PROGRESS_FILE = f"{OUT}/m3_fspf_v1_progress.json"
SCORECARD_FILE = f"{OUT}/m3_fspf_v1_scorecard.json"
RFQ_QUEUE_FILE = f"{OUT}/m3_fspf_v1_rfq_queue.json"
STOP_FILE = f"{OUT}/m3_fspf_v1_stop.json"
REPORT_FILE = f"{OUT}/m3_fspf_v1_report.json"
CIRCUIT_FILE = f"{OUT}/m3_fspf_v1_component_circuits.json"

SLICE_SIZE = int(os.environ.get("M3_FSPF_SLICE_SIZE") or "25")
# 100s covers PG registration (inspect/fill/save) + detail retry inside CFE
ITEM_TIMEOUT_S = float(os.environ.get("M3_FSPF_ITEM_TIMEOUT_S") or "100")
# Concurrent full (browser) opportunity funnels — keep low; fast-path is sequential/cheap
BROWSER_CONCURRENCY = max(1, min(4, int(os.environ.get("M3_FSPF_BROWSER_CONCURRENCY") or "2")))
# Lease staleness for fencing — independent supervisor recovers at ~120s of
# zero progress. Keep lease window slightly above ITEM_TIMEOUT_S + heartbeat.
STALE_LEASE_S = float(
    os.environ.get("M3_FSPF_STALE_LEASE_S")
    or max(
        float(LONG_RUNNING_JOB_EXECUTION_STANDARD.get("STALE_RUNNING_SECONDS") or 120),
        float(ITEM_TIMEOUT_S) + 120.0,
        300.0,
    )
)
# Independent heartbeat while a per-opportunity future is in flight.
WORKER_HEARTBEAT_S = float(os.environ.get("M3_FSPF_WORKER_HEARTBEAT_S") or "12")
SOURCE_COOLDOWN_S = float(os.environ.get("M3_FSPF_SOURCE_COOLDOWN_S") or "0.4")
AUTO_SEND = False

_COMMON = re.compile(
    r"\b(tool|tools|screw|fastener|mro|lubricant|filter|toner|paper|janitorial|"
    r"ppe|glove|hardware|electrical|lighting|lamp|led|office|furniture|automotive|"
    r"windshield|cable|battery|hose|valve|bearing|abrasive|safety|supply|supplies)\b",
    re.I,
)
_COMPLEX = re.compile(
    r"\b(construction|renovation|install(ation)?|labor|service\s+only|consulting|"
    r"software\s+license|staffing|custodial\s+service)\b",
    re.I,
)

_worker = False
_stop_requested = False
_thread_lock = threading.Lock()

# Terminal item results that count as "processed" (documented outcome)
_TERMINAL = {
    "SUCCESS",
    "PARTIAL",
    "TIMEOUT",
    "AUTH_FAILED",
    "NO_PACKAGE",
    "FETCH_FAILED",
    "PARSE_FAILED",
    "SKIPPED_ALREADY_COMPLETE",
    "EXPIRED_EXIT",
    "INELIGIBLE_EXIT",
    "BUDGET_HOLD",
    "OWNER_ACTION_REQUIRED",
}


def _item_should_skip(prev: dict[str, Any] | None, *, first_pass: bool = True) -> bool:
    """Skip durable outcomes. First-pass never inline-retries software errors."""
    if not prev:
        return False
    result = str(prev.get("RESULT") or "")
    # Already attempted in first pass → skip until deferred recovery
    if first_pass and FIRST_PASS_NO_INLINE_RETRY:
        if result in _PRESERVE_RESULTS:
            return True
        if result in {
            "DEFERRED_ERROR",
            "DEFERRED_EXTERNAL",
            "TIMEOUT",
            "NO_PACKAGE",
            "FETCH_FAILED",
            "PARSE_FAILED",
            "AUTH_FAILED",
            "PARTIAL",
            "SUCCESS",
            "OWNER_ACTION_REQUIRED",
            "EXPIRED_EXIT",
            "INELIGIBLE_EXIT",
            "PENDING_RETRY",  # treat as already-attempted; deferred owns retries
        }:
            return True
        if result in _TERMINAL:
            return True
        return False
    if result == "PENDING_RETRY" or result == "DEFERRED_ERROR":
        return int(prev.get("RETRY_COUNT") or 0) >= MAX_TECHNICAL_RETRIES
    if result in _PRESERVE_RESULTS:
        return True
    if (
        result == "PARTIAL"
        and int(prev.get("fully_specified_product_lines") or 0) > 0
        and int(prev.get("documents_downloaded") or 0) > 0
    ):
        return True
    if prev.get("RETRYABLE") and int(prev.get("RETRY_COUNT") or 0) < MAX_TECHNICAL_RETRIES:
        return False
    return result in _TERMINAL


def _load_deferred_queue() -> dict[str, Any]:
    return _load(DEFERRED_QUEUE_FILE) or {
        "kind": "FullScaleDeferredQueue",
        "build": BUILD,
        "items": [],
        "by_defect": {},
        "updated_at": _utc(),
    }


def _defer_opportunity(
    *,
    opportunity_id: str,
    stage: str | None,
    outcome: str,
    defect_key: str | None,
    shared_access_key: str | None,
    blocker: str | None,
    index: int | None,
    recovery_eligible: bool | None = None,
    pg_unlocked: str | None = None,
    next_stage: str | None = None,
    last_completed_stage: str | None = None,
    commercial_resume: bool | None = None,
) -> None:
    """Append to back-of-line deferred queue (idempotent per oid).

    Preserves PG-unlock recovery_eligible / pg_unlocked when re-deferring an
    already-unlocked opportunity so failed browser attempts do not drop it from
    the unlock recovery set.
    """
    q = _load_deferred_queue()
    prior = next(
        (
            x
            for x in (q.get("items") or [])
            if isinstance(x, dict) and str(x.get("opportunity_id")) == str(opportunity_id)
        ),
        None,
    )
    items = [x for x in (q.get("items") or []) if str(x.get("opportunity_id")) != opportunity_id]
    prior_eligible = bool((prior or {}).get("recovery_eligible"))
    prior_pg = str((prior or {}).get("pg_unlocked") or "") or None
    elig = prior_eligible if recovery_eligible is None else bool(recovery_eligible)
    pg = (str(pg_unlocked).strip() if pg_unlocked else None) or prior_pg
    if elig and not pg and shared_access_key and str(shared_access_key).startswith("pg:"):
        pg = str(shared_access_key).split(":", 1)[1].strip() or None
    entry: dict[str, Any] = {
        "opportunity_id": opportunity_id,
        "stage_reached": stage,
        "funnel_outcome": outcome,
        "software_defect_key": defect_key,
        "shared_access_key": shared_access_key
        or ((prior or {}).get("shared_access_key") if elig else None),
        "terminal_blocker": (blocker or "")[:160] or None,
        "queue_index": index if index is not None else (prior or {}).get("queue_index"),
        "deferred_at": _utc(),
        "next_stage": next_stage or (prior or {}).get("next_stage"),
        "last_completed_stage": last_completed_stage
        or (prior or {}).get("last_completed_stage"),
        "commercial_resume": bool(
            commercial_resume
            if commercial_resume is not None
            else (prior or {}).get("commercial_resume")
        )
        or False,
    }
    # Always retain the purchasing-group id when known — REGISTERED membership is
    # durable even if this solicitation needs an additional paid entitlement and
    # must leave the READY lane.
    if pg:
        entry["pg_unlocked"] = pg
        if not entry.get("shared_access_key"):
            entry["shared_access_key"] = f"pg:{pg}"
    if elig:
        entry["recovery_eligible"] = True
        entry["unlocked_at"] = (prior or {}).get("unlocked_at") or _utc()
        # Keep unlock outcome stable so deferred-pass preferencing still works.
        if not entry.get("funnel_outcome") or str(entry.get("funnel_outcome")) in {
            "DEFERRED_EXTERNAL",
            "DEFERRED_ERROR",
            "TIMEOUT",
            "EXTERNAL_DEPENDENCY",
        }:
            entry["funnel_outcome"] = "PG_UNLOCK_RECOVERY"
    else:
        entry["recovery_eligible"] = False
    items.append(entry)
    q["items"] = items[-20000:]
    by_def = dict(q.get("by_defect") or {})
    if defect_key:
        by_def[defect_key] = int(by_def.get(defect_key) or 0) + 1
    q["by_defect"] = by_def
    q["updated_at"] = _utc()
    q["build"] = BUILD
    q["count"] = len(q["items"])
    q["pg_unlock_eligible_count"] = sum(
        1 for x in q["items"] if isinstance(x, dict) and x.get("recovery_eligible")
    )
    _save(DEFERRED_QUEUE_FILE, q)


def _component_circuit_tripped(defect_key: str | None) -> bool:
    if not defect_key:
        return False
    circ = _load(CIRCUIT_FILE) or {"defects": {}}
    row = (circ.get("defects") or {}).get(defect_key) or {}
    return bool(row.get("open")) and int(row.get("count") or 0) >= 8


def _note_component_defect(defect_key: str | None) -> dict[str, Any]:
    if not defect_key:
        return {}
    circ = _load(CIRCUIT_FILE) or {"kind": "FSPFComponentCircuits", "defects": {}}
    defects = dict(circ.get("defects") or {})
    row = dict(defects.get(defect_key) or {"count": 0, "open": False})
    row["count"] = int(row.get("count") or 0) + 1
    row["last_at"] = _utc()
    if row["count"] >= 8:
        row["open"] = True
        row["note"] = "component_circuit_open_pause_same_defect"
    defects[defect_key] = row
    circ["defects"] = defects
    circ["updated_at"] = _utc()
    _save(CIRCUIT_FILE, circ)
    return row


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> Path:
    """Write JSON with volume-safe fallback (see m3_jobs.store._atomic_write)."""
    from m3_jobs.store import _atomic_write

    path = Path(data_path(rel))
    _atomic_write(path, obj)
    return path


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> dict[str, Any]:
    cur = _load(PROGRESS_FILE) or {
        "kind": "FullScaleProductionFunnelProgress",
        "build": BUILD,
        "status": "IDLE",
    }
    # Zombie workers (lease superseded) must not clobber the live worker's progress
    # with STOPPED / LEASE_SUPERSEDED — that falsely looks like production halted.
    incoming_worker = str(kwargs.get("worker_id") or "")
    incoming_status = str(kwargs.get("status") or "")
    incoming_phase = str(kwargs.get("phase") or "")
    if incoming_worker and (
        incoming_status in {"STOPPED", "CANCELLED"}
        or incoming_phase == "LEASE_SUPERSEDED"
    ):
        jid = str(kwargs.get("job_id") or cur.get("job_id") or "")
        if jid and not _is_active_lease_worker(jid, incoming_worker):
            return cur
    cur.update(kwargs)
    cur["build"] = BUILD
    cur["heartbeat_at"] = _utc()
    cur["AUTO_SEND"] = AUTO_SEND
    _save(PROGRESS_FILE, cur)
    return cur


def job_progress() -> dict[str, Any]:
    prog = _load(PROGRESS_FILE) or {"status": "IDLE", "build": BUILD}
    store = JobStore(FAMILY)
    # Read-only — progress polling must never mutate RUNNING → STALE_RELEASED.
    active = store.find_active_job(stale_seconds=STALE_LEASE_S, mutate_stale=False)
    payload: dict[str, Any] = {
        **prog,
        "build": BUILD,
        "LONG_RUNNING_JOB_EXECUTION_STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "active_job": None,
    }
    if active:
        try:
            payload["active_job"] = store.progress_payload(str(active["JOB_ID"]))
        except Exception as exc:
            payload["active_job_error"] = type(exc).__name__
    return payload


def request_stop(*, force_release_stale: bool = True) -> dict[str, Any]:
    """Request graceful stop; release stale exclusive lease so resume can acquire."""
    global _stop_requested, _worker
    _stop_requested = True
    _save(STOP_FILE, {"stop": True, "requested_at": _utc()})
    pg_mgr_stop = None
    try:
        from bidnet_auth.purchasing_group_access_manager_v1 import request_stop as pg_mgr_request_stop

        pg_mgr_stop = pg_mgr_request_stop()
    except Exception as exc:
        pg_mgr_stop = {"error": type(exc).__name__}
    store = JobStore(FAMILY)
    active = store.find_active_job(stale_seconds=STALE_LEASE_S)
    released = None
    if active and force_release_stale:
        jid = str(active["JOB_ID"])
        st = str(active.get("STATUS") or "")
        if st in {"RUNNING", "STALE_RELEASED", "INTERRUPTED"}:
            try:
                released = store.force_reset_running_lock(jid)
                _worker = False
                _progress(
                    status="STOPPED",
                    phase="FORCE_RELEASED_STALE",
                    job_id=jid,
                    lease_released=True,
                )
            except Exception as exc:
                released = {"error": type(exc).__name__}
    return {
        "status": "STOP_REQUESTED",
        "build": BUILD,
        "pg_access_manager_stop": pg_mgr_stop,
        "lease_release": released,
        "progress": job_progress(),
    }


def _pg_id_from_item(it: dict[str, Any]) -> str:
    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url

    pg = str(it.get("purchasing_group_id") or "").strip()
    if pg:
        return pg
    sak = str(it.get("shared_access_key") or "")
    if sak.startswith("pg:"):
        return sak.split(":", 1)[1].strip()
    return (
        extract_pg_id_from_url(str(it.get("authoritative_url") or it.get("AUTHORITATIVE_URL") or ""))
        or ""
    )


def release_pg_blocked_for_registered_groups(job_id: str) -> dict[str, Any]:
    """Mark deferred/blocked opportunities eligible for recovery after PG REGISTERED.

    Does NOT reset RESUME_CURSOR — first-pass continues uninterrupted. Matching
    checkpoints are PENDING_RETRY + recovery_eligible and are also written into the
    deferred queue so post-first-pass recovery cannot lose them.
    """
    from bidnet_auth.purchasing_group_tracker import is_registered, load_tracker

    store = JobStore(FAMILY)
    job = store.load_job(job_id)
    if not job:
        raise KeyError(job_id)
    tracker = load_tracker()
    registered = {
        str(pg)
        for pg, g in (tracker.get("groups") or {}).items()
        if isinstance(g, dict) and g.get("status") == "REGISTERED"
    }
    marked = 0
    examined = 0
    deferred_enqueued = 0
    for it in store.list_items(job_id):
        if not isinstance(it, dict):
            continue
        examined += 1
        blocker = str(it.get("terminal_blocker") or it.get("TERMINAL_BLOCKER") or "")
        result = str(it.get("RESULT") or "")
        if result in _PRESERVE_RESULTS and result != "OWNER_ACTION_REQUIRED":
            continue
        # Already successfully completed after unlock — leave alone
        if result in {"SUCCESS", "PARTIAL", "SKIPPED_ALREADY_COMPLETE"} and it.get(
            "PG_UNLOCK_ELIGIBLE"
        ):
            continue
        pg = _pg_id_from_item(it)
        if not pg:
            continue
        if pg not in registered and not is_registered(pg):
            continue
        blocker_ok = blocker in {
            "PURCHASING_GROUP_REGISTRATION_REQUIRED",
            "AWAITING_PURCHASING_GROUP_REGISTRATION",
            "AWAITING_OWNER_ACTION",
        } or "PURCHASING" in blocker.upper()
        result_ok = result in {
            "NO_PACKAGE",
            "OWNER_ACTION_REQUIRED",
            "PENDING_RETRY",
            "DEFERRED_EXTERNAL",
            "DEFERRED_ERROR",
            "TIMEOUT",
        }
        already_flagged = bool(it.get("PG_UNLOCK_ELIGIBLE") or it.get("recovery_eligible"))
        if not blocker_ok and not result_ok and not already_flagged:
            continue
        oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "").strip()
        if not oid:
            continue
        if result != "PENDING_RETRY" or not already_flagged:
            it["PREVIOUS_RESULT"] = result
            it["PREVIOUS_BLOCKER"] = blocker
            it["RETRYABLE"] = True
            it["RESULT"] = "PENDING_RETRY"
            it["RETRY_QUEUED_AT"] = _utc()
            it["NOTE"] = f"Released after purchasing group {pg} REGISTERED"
            it["purchasing_group_id"] = pg
            it["PG_UNLOCK_ELIGIBLE"] = True
            it["recovery_eligible"] = True
            it["shared_access_key"] = it.get("shared_access_key") or f"pg:{pg}"
            if not it.get("PG_UNLOCK_RETRY"):
                it["PG_UNLOCK_RETRY"] = True
                it["RETRY_COUNT"] = 0
            store.write_item_checkpoint(job_id, it)
            marked += 1
        # Always ensure deferred-queue membership (idempotent) for post-first-pass.
        _defer_opportunity(
            opportunity_id=oid,
            stage=it.get("stage_reached") or it.get("STAGE_REACHED") or "ACCESS_DOCS",
            outcome="PG_UNLOCK_RECOVERY",
            defect_key=None,
            shared_access_key=f"pg:{pg}",
            blocker="AWAITING_PURCHASING_GROUP_REGISTRATION_RESOLVED",
            index=int(it.get("INDEX") if it.get("INDEX") is not None else -1),
        )
        # Stamp recovery flags on the deferred row
        try:
            dq = _load_deferred_queue()
            for d in dq.get("items") or []:
                if str(d.get("opportunity_id")) == oid:
                    d["recovery_eligible"] = True
                    d["pg_unlocked"] = pg
                    d["unlocked_at"] = d.get("unlocked_at") or _utc()
                    deferred_enqueued += 1
                    break
            _save(DEFERRED_QUEUE_FILE, dq)
        except Exception:
            pass

    if marked or deferred_enqueued:
        job = store.load_job(job_id) or job
        # Do NOT reset RESUME_CURSOR — that would interrupt unfinished first-pass work.
        meta = dict(job.get("META") or {})
        meta["pg_unlock_released"] = int(meta.get("pg_unlock_released") or 0) + marked
        meta["pg_unlock_deferred_enqueued"] = int(
            meta.get("pg_unlock_deferred_enqueued") or 0
        ) + deferred_enqueued
        meta["pg_unlock_at"] = _utc()
        meta["repair_build"] = BUILD
        job["META"] = meta
        store.save_job(job)
        store.recompute_counts(job_id)
        # Promote unlock-eligible into READY priority lane (does not reset cursor).
        try:
            from buyer_intelligence.fspf_ready_lane_v1 import promote_unlock_eligible_to_ready

            promote_unlock_eligible_to_ready(job_id=job_id)
        except Exception:
            pass
        # Promote unlock-eligible into READY priority lane (does not reset cursor).
        try:
            from buyer_intelligence.fspf_ready_lane_v1 import promote_unlock_eligible_to_ready

            promote_unlock_eligible_to_ready(job_id=job_id)
        except Exception:
            pass
    return {
        "job_id": job_id,
        "registered_groups": sorted(registered),
        "examined": examined,
        "released": marked,
        "deferred_enqueued": deferred_enqueued,
        "build": BUILD,
    }


def reconcile_released_pg_unlocks(
    job_id: str | None = None,
    *,
    repair: bool = True,
) -> dict[str, Any]:
    """Account for every PG-unlock released opportunity. Optionally restore orphans.

    Valid states:
      completed — SUCCESS/PARTIAL/SKIPPED after unlock (or durable success)
      recovery_scheduled — PENDING_RETRY + in deferred queue (post-first-pass)
      pending_first_pass — not yet attempted (should be rare for released)
      currently_processing — RESULT empty / in-flight
      rejected_or_expired — EXPIRED/INELIGIBLE/OWNER durable
      orphaned — unlock-eligible but missing from deferred queue (repaired when repair=True)
    """
    from bidnet_auth.purchasing_group_tracker import is_registered, load_tracker

    store = JobStore(FAMILY)
    if not job_id:
        active = store.find_active_job(stale_seconds=86400)
        job_id = str((active or {}).get("JOB_ID") or "")
    if not job_id:
        return {"ok": False, "error": "job_id_required", "build": BUILD}
    job = store.load_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found", "job_id": job_id, "build": BUILD}

    tracker = load_tracker()
    registered = {
        str(pg)
        for pg, g in (tracker.get("groups") or {}).items()
        if isinstance(g, dict) and g.get("status") == "REGISTERED"
    }
    # Seed released set from tracker blocked lists + checkpoints
    from_tracker: set[str] = set()
    for pg in registered:
        g = (tracker.get("groups") or {}).get(pg) or {}
        for oid in g.get("blocked_opportunity_ids") or []:
            if oid:
                from_tracker.add(str(oid))

    dq = _load_deferred_queue()
    deferred_by_oid = {
        str(x.get("opportunity_id")): x
        for x in (dq.get("items") or [])
        if isinstance(x, dict) and x.get("opportunity_id")
    }

    buckets: dict[str, list[dict[str, Any]]] = {
        "completed": [],
        "recovery_scheduled": [],
        "pending_first_pass": [],
        "currently_processing": [],
        "rejected_or_expired": [],
        "orphaned": [],
        "still_blocked_unregistered_pg": [],
    }
    released_ids: list[str] = []
    repaired = 0
    by_pg: dict[str, int] = {}

    for it in store.list_items(job_id):
        if not isinstance(it, dict):
            continue
        oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "").strip()
        if not oid:
            continue
        pg = _pg_id_from_item(it)
        note = str(it.get("NOTE") or "")
        is_released = bool(
            it.get("PG_UNLOCK_ELIGIBLE")
            or it.get("PG_UNLOCK_RETRY")
            or "Released after purchasing group" in note
            or (pg in registered and oid in from_tracker)
        )
        # Also catch registered-PG blockers not yet flagged
        result = str(it.get("RESULT") or "")
        blocker = str(it.get("terminal_blocker") or it.get("TERMINAL_BLOCKER") or "")
        sak = str(it.get("shared_access_key") or "")
        should_be_released = bool(
            pg
            and pg in registered
            and (
                result
                in {
                    "DEFERRED_EXTERNAL",
                    "PENDING_RETRY",
                    "NO_PACKAGE",
                    "TIMEOUT",
                    "DEFERRED_ERROR",
                    "OWNER_ACTION_REQUIRED",
                }
                or "PURCHASING" in blocker.upper()
                or sak == f"pg:{pg}"
            )
            and result not in {"SUCCESS", "PARTIAL", "SKIPPED_ALREADY_COMPLETE"}
        )
        if not is_released and not should_be_released:
            continue
        if not is_released and should_be_released:
            is_released = True
        released_ids.append(oid)
        by_pg[pg or "?"] = by_pg.get(pg or "?", 0) + 1
        row = {
            "opportunity_id": oid,
            "result": result or None,
            "purchasing_group_id": pg or None,
            "stage_reached": it.get("stage_reached") or it.get("STAGE_REACHED"),
            "in_deferred_queue": oid in deferred_by_oid,
            "recovery_eligible": bool(
                it.get("recovery_eligible")
                or (deferred_by_oid.get(oid) or {}).get("recovery_eligible")
            ),
        }
        if result in {"SUCCESS", "PARTIAL", "SKIPPED_ALREADY_COMPLETE"}:
            buckets["completed"].append(row)
        elif result in {"EXPIRED_EXIT", "INELIGIBLE_EXIT"}:
            buckets["rejected_or_expired"].append(row)
        elif result == "OWNER_ACTION_REQUIRED" and not it.get("PG_UNLOCK_ELIGIBLE"):
            buckets["rejected_or_expired"].append(row)
        elif not result or result in {"RUNNING", "IN_PROGRESS"}:
            buckets["currently_processing"].append(row)
        elif pg and pg not in registered and not is_registered(pg):
            buckets["still_blocked_unregistered_pg"].append(row)
        elif oid in deferred_by_oid or (
            result == "PENDING_RETRY" and it.get("PG_UNLOCK_ELIGIBLE")
        ):
            # Ensure deferred membership
            if oid not in deferred_by_oid and repair:
                _defer_opportunity(
                    opportunity_id=oid,
                    stage=row["stage_reached"] or "ACCESS_DOCS",
                    outcome="PG_UNLOCK_RECOVERY",
                    defect_key=None,
                    shared_access_key=f"pg:{pg}" if pg else it.get("shared_access_key"),
                    blocker="AWAITING_PURCHASING_GROUP_REGISTRATION_RESOLVED",
                    index=int(it.get("INDEX") if it.get("INDEX") is not None else -1),
                )
                dq2 = _load_deferred_queue()
                for d in dq2.get("items") or []:
                    if str(d.get("opportunity_id")) == oid:
                        d["recovery_eligible"] = True
                        d["pg_unlocked"] = pg
                        d["unlocked_at"] = _utc()
                _save(DEFERRED_QUEUE_FILE, dq2)
                deferred_by_oid[oid] = {"opportunity_id": oid}
                repaired += 1
                row["in_deferred_queue"] = True
                row["repaired"] = True
            buckets["recovery_scheduled"].append(row)
        else:
            buckets["orphaned"].append(row)
            if repair:
                # Restore unlock flags + deferred membership
                it["RESULT"] = "PENDING_RETRY"
                it["RETRYABLE"] = True
                it["PG_UNLOCK_ELIGIBLE"] = True
                it["recovery_eligible"] = True
                it["purchasing_group_id"] = pg
                it["shared_access_key"] = it.get("shared_access_key") or (f"pg:{pg}" if pg else None)
                it["NOTE"] = f"Reconciled unlock after purchasing group {pg} REGISTERED"
                it["RETRY_QUEUED_AT"] = _utc()
                if not it.get("PG_UNLOCK_RETRY"):
                    it["PG_UNLOCK_RETRY"] = True
                    it["RETRY_COUNT"] = 0
                store.write_item_checkpoint(job_id, it)
                _defer_opportunity(
                    opportunity_id=oid,
                    stage=row["stage_reached"] or "ACCESS_DOCS",
                    outcome="PG_UNLOCK_RECOVERY",
                    defect_key=None,
                    shared_access_key=f"pg:{pg}" if pg else None,
                    blocker="AWAITING_PURCHASING_GROUP_REGISTRATION_RESOLVED",
                    index=int(it.get("INDEX") if it.get("INDEX") is not None else -1),
                )
                dq2 = _load_deferred_queue()
                for d in dq2.get("items") or []:
                    if str(d.get("opportunity_id")) == oid:
                        d["recovery_eligible"] = True
                        d["pg_unlocked"] = pg
                        d["unlocked_at"] = _utc()
                _save(DEFERRED_QUEUE_FILE, dq2)
                repaired += 1
                row["repaired"] = True
                buckets["recovery_scheduled"].append(row)
                buckets["orphaned"].pop()

    # Also include tracker-blocked IDs that have no checkpoint yet (awaiting first pass)
    seen = set(released_ids)
    for oid in from_tracker:
        if oid in seen:
            continue
        prev = store.load_item(job_id, oid)
        if prev:
            continue
        # Never attempted — still first-pass eligible; not "released from deferral"
        buckets["pending_first_pass"].append(
            {"opportunity_id": oid, "result": None, "note": "tracker_blocked_unattempted"}
        )
        released_ids.append(oid)

    # Deduplicate released_ids preserving order
    uniq: list[str] = []
    seen2: set[str] = set()
    for oid in released_ids:
        if oid not in seen2:
            seen2.add(oid)
            uniq.append(oid)

    # Prefer recovery-eligible items at front of deferred queue (still after first-pass)
    if repair:
        dq = _load_deferred_queue()
        items = list(dq.get("items") or [])
        unlock = [x for x in items if isinstance(x, dict) and x.get("recovery_eligible")]
        other = [x for x in items if not (isinstance(x, dict) and x.get("recovery_eligible"))]
        dq["items"] = unlock + other
        dq["count"] = len(dq["items"])
        dq["pg_unlock_eligible_count"] = len(unlock)
        dq["reconciled_at"] = _utc()
        dq["build"] = BUILD
        _save(DEFERRED_QUEUE_FILE, dq)

    summary = {k: len(v) for k, v in buckets.items()}
    accounted = (
        summary["completed"]
        + summary["recovery_scheduled"]
        + summary["pending_first_pass"]
        + summary["currently_processing"]
        + summary["rejected_or_expired"]
        + summary["still_blocked_unregistered_pg"]
    )
    # After repair, orphans should be 0 (moved into recovery_scheduled)
    orphan_remaining = summary["orphaned"]
    report = {
        "ok": True,
        "build": BUILD,
        "job_id": job_id,
        "registered_groups": sorted(registered),
        "released_opportunity_ids_count": len(uniq),
        "released_by_pg": by_pg,
        "summary": summary,
        "fully_accounted": orphan_remaining == 0,
        "missing_or_orphaned": orphan_remaining,
        "accounted_non_orphan": accounted,
        "repaired": repaired,
        "deferred_queue_size": len((_load_deferred_queue().get("items") or [])),
        "recovery_eligible_in_deferred": int(
            (_load_deferred_queue() or {}).get("pg_unlock_eligible_count") or 0
        ),
        "sample_completed": buckets["completed"][:8],
        "sample_recovery": buckets["recovery_scheduled"][:8],
        "sample_orphaned": buckets["orphaned"][:8],
        "sample_rejected": buckets["rejected_or_expired"][:8],
        "pass_mode": (job.get("META") or {}).get("pass_mode") or "FIRST_PASS",
        "resume_cursor": job.get("RESUME_CURSOR"),
        "note": (
            "Recovery runs automatically after first-pass exhaustion; "
            "unlocked IDs are queued but do not interrupt untouched first-pass work."
        ),
    }
    try:
        _save(
            f"{OUT}/m3_fspf_v1_released_unlock_reconcile.json",
            {**report, "released_opportunity_ids": uniq[:5000], "at": _utc()},
        )
    except Exception:
        pass
    return report


def _recover_one_unlocked_opportunity(
    *,
    job_id: str,
    worker_id: str,
    rows: list[dict[str, Any]],
    store: Any,
    http: Any,
    evidence_index: Any,
    l23: dict[str, Any],
) -> dict[str, Any]:
    """Process one PG-unlock recovery_eligible item when the browser is free.

    Does not advance RESUME_CURSOR or steal unattempted first-pass slots.
    """
    from bidnet_auth.browser_mutex_v1 import bidnet_browser_slot
    from buyer_intelligence.commercial_funnel_execution_v1 import _open_bidnet_auth_client
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import run_one_opportunity

    dq = _load_deferred_queue()
    candidates = [
        d
        for d in (dq.get("items") or [])
        if isinstance(d, dict) and d.get("recovery_eligible") and d.get("opportunity_id")
    ]
    if not candidates:
        return {"processed": 0, "reason": "no_recovery_eligible"}

    # Prefer commercial-resume (extract already done) over fresh access retries.
    def _cand_rank(d: dict[str, Any]) -> tuple[int, int]:
        nxt = str(d.get("next_stage") or "").upper()
        commercial = 0 if d.get("commercial_resume") or nxt in {"SOURCE", "PRICE"} else 1
        return (commercial, 0)

    candidates = sorted(candidates, key=_cand_rank)

    picked = None
    for d in candidates:
        oid = str(d.get("opportunity_id"))
        prev = store.load_item(job_id, oid)
        if prev and str(prev.get("RESULT") or "") in _PRESERVE_RESULTS | {
            "SUCCESS",
            "PARTIAL",
            "SKIPPED_ALREADY_COMPLETE",
        }:
            continue
        picked = d
        break
    if not picked:
        return {"processed": 0, "reason": "no_eligible_candidate"}

    oid = str(picked.get("opportunity_id"))
    idx = int(picked.get("queue_index") or -1)
    item = rows[idx] if 0 <= idx < len(rows) else {"opportunity_id": oid}
    if str(item.get("opportunity_id") or "") != oid:
        item = {"opportunity_id": oid}
        for j, r in enumerate(rows):
            if str(r.get("opportunity_id")) == oid:
                item = r
                idx = j
                break

    # Enrich from deferred entry + prior checkpoint so recovery does not drop URL/PG.
    prior_cp = store.load_item(job_id, oid) or {}
    pg_unlocked = str(
        picked.get("pg_unlocked")
        or prior_cp.get("purchasing_group_id")
        or ""
    ).strip() or None
    item = dict(item)
    item["opportunity_id"] = oid
    item["authoritative_url"] = (
        item.get("authoritative_url")
        or prior_cp.get("authoritative_url")
        or prior_cp.get("AUTHORITATIVE_URL")
    )
    item["title"] = item.get("title") or prior_cp.get("title")
    item["buyer"] = item.get("buyer") or prior_cp.get("buyer")
    item["purchasing_group_id"] = pg_unlocked or item.get("purchasing_group_id")
    item["shared_access_key"] = (
        picked.get("shared_access_key")
        or prior_cp.get("shared_access_key")
        or (f"pg:{pg_unlocked}" if pg_unlocked else None)
    )
    item["stage_reached"] = (
        picked.get("stage_reached") or prior_cp.get("stage_reached") or "ACCESS_DOCS"
    )
    item["recovery_eligible"] = True
    item["PG_UNLOCK_ELIGIBLE"] = True
    item["_prior_checkpoint"] = prior_cp
    item["commercial_resume"] = bool(
        picked.get("commercial_resume") or prior_cp.get("commercial_resume")
    )
    item["next_stage"] = (
        picked.get("next_stage")
        or prior_cp.get("next_stage")
        or item.get("stage_reached")
    )
    item["last_completed_stage"] = picked.get("last_completed_stage") or prior_cp.get(
        "last_completed_stage"
    )
    if prior_cp.get("line_items"):
        item["line_items"] = prior_cp.get("line_items")
    if prior_cp.get("recovered_documents"):
        item["recovered_documents"] = prior_cp.get("recovered_documents")
    if prior_cp.get("complete_spec"):
        item["complete_spec"] = True
    if prior_cp.get("documents_downloaded"):
        item["documents_downloaded"] = prior_cp.get("documents_downloaded")
    # Hydrate product lines from RFQ queue when older checkpoints omitted them.
    if not item.get("line_items"):
        try:
            rfq_blob = _load(RFQ_QUEUE_FILE) or {}
            for entry in rfq_blob.get("items") or rfq_blob.get("rows") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("opportunity_id") or "") != oid:
                    continue
                rfq = entry.get("rfq") if isinstance(entry.get("rfq"), dict) else entry
                lines = list(rfq.get("line_items") or entry.get("line_items") or [])
                if lines:
                    item["line_items"] = lines
                    item["complete_spec"] = True
                    item.setdefault("documents_downloaded", max(1, int(prior_cp.get("documents_downloaded") or 0)))
                    break
        except Exception:
            pass

    # Keep exclusive job lease alive across browser wait + funnel (can exceed 2m).
    try:
        store.heartbeat(
            job_id,
            worker_id=worker_id,
            current_index=max(0, idx),
            current_opportunity_id=oid,
        )
        _progress(
            status="RUNNING",
            phase="PG_UNLOCK_MICRO_RECOVERY",
            job_id=job_id,
            worker_id=worker_id,
            current_opportunity_id=oid,
            note="unlock_micro_recovery_acquired_candidate",
            resume_cursor=(store.load_job(job_id) or {}).get("RESUME_CURSOR"),
        )
    except Exception:
        pass

    auth_client = None
    row: dict[str, Any] = {}
    # Commercial resume with durable extract: skip Playwright re-open when possible.
    commercial_only = bool(
        item.get("commercial_resume")
        and item.get("complete_spec")
        and item.get("line_items")
        and int(item.get("documents_downloaded") or 0) > 0
        and str(item.get("next_stage") or "").upper()
        in {"SOURCE", "PRICE", "ECONOMICS", "FINANCE", "BID_PREP"}
    )
    if commercial_only:
        try:
            auth_status = {"authenticated": False, "status": "SKIPPED_COMMERCIAL_RESUME"}
            row = run_one_opportunity(
                item,
                client=http,
                evidence_index=evidence_index,
                auth_status=auth_status,
                l23_rec=(l23 or {}).get(oid) or {},
                auth_client=None,
            )
            result = _map_result(row)
            slim = _slim_checkpoint(row, result=result, index=max(0, idx), prior=prior_cp)
            slim["PG_UNLOCK_RECOVERY_AT"] = _utc()
            slim["recovery_pass"] = "MICRO_COMMERCIAL_RESUME"
            slim["PG_UNLOCK_ELIGIBLE"] = True
            slim["purchasing_group_id"] = (
                slim.get("purchasing_group_id") or pg_unlocked or row.get("purchasing_group_id")
            )
            store.write_item_checkpoint(job_id, slim)
            _append_rfq(row)
            if result in _PRESERVE_RESULTS | {"SUCCESS", "PARTIAL"}:
                dq2 = _load_deferred_queue()
                dq2["items"] = [
                    x for x in (dq2.get("items") or []) if str(x.get("opportunity_id")) != oid
                ]
                dq2["count"] = len(dq2["items"])
                dq2["pg_unlock_eligible_count"] = sum(
                    1 for x in dq2["items"] if isinstance(x, dict) and x.get("recovery_eligible")
                )
                _save(DEFERRED_QUEUE_FILE, dq2)
            elif row.get("defer") or result in {"DEFERRED_ERROR", "DEFERRED_EXTERNAL", "TIMEOUT"}:
                _defer_opportunity(
                    opportunity_id=oid,
                    stage=row.get("stage_reached") or "SOURCE",
                    outcome=str(row.get("funnel_outcome") or result),
                    defect_key=row.get("software_defect_key"),
                    shared_access_key=row.get("shared_access_key")
                    or picked.get("shared_access_key"),
                    blocker=row.get("terminal_blocker"),
                    index=idx,
                    recovery_eligible=True,
                    pg_unlocked=pg_unlocked,
                    next_stage=row.get("next_stage"),
                    last_completed_stage=row.get("last_completed_stage"),
                    commercial_resume=bool(row.get("commercial_resume")),
                )
            return {
                "processed": 1,
                "opportunity_id": oid,
                "purchasing_group_id": pg_unlocked or slim.get("purchasing_group_id"),
                "result": result,
                "stage_reached": slim.get("stage_reached"),
                "next_stage": slim.get("next_stage"),
                "commercial_resume": True,
                "skipped_browser": True,
                "documents_downloaded": slim.get("documents_downloaded"),
                "fully_specified_product_lines": slim.get("fully_specified_product_lines"),
                "public_unit_price": slim.get("public_unit_price"),
            }
        except Exception as exc:
            # Fall through to browser path if commercial-only resume fails hard.
            row = {"opportunity_id": oid, "terminal_blocker": type(exc).__name__}

    with bidnet_browser_slot(holder=f"unlock-{worker_id}", timeout_s=20.0, ttl_s=240.0) as got:
        if not got:
            return {"processed": 0, "reason": "browser_slot_busy", "opportunity_id": oid}
        try:
            try:
                store.heartbeat(
                    job_id,
                    worker_id=worker_id,
                    current_index=max(0, idx),
                    current_opportunity_id=oid,
                )
            except Exception:
                pass
            auth_status, auth_client = _open_bidnet_auth_client()
            if not auth_status.get("authenticated") or auth_client is None:
                return {
                    "processed": 0,
                    "reason": "auth_unavailable",
                    "opportunity_id": oid,
                    "auth_status": auth_status.get("status"),
                }
            try:
                store.heartbeat(
                    job_id,
                    worker_id=worker_id,
                    current_index=max(0, idx),
                    current_opportunity_id=oid,
                )
            except Exception:
                pass
            row = run_one_opportunity(
                item,
                client=http,
                evidence_index=evidence_index,
                auth_status=auth_status,
                l23_rec=(l23 or {}).get(oid) or {},
                auth_client=auth_client,
            )
        except Exception as exc:
            row = {
                "opportunity_id": oid,
                "terminal_blocker": type(exc).__name__,
                "funnel_outcome": "SOFTWARE_ERROR",
                "stage_reached": picked.get("stage_reached") or "ACCESS_DOCS",
                "defer": True,
                "software_defect_key": type(exc).__name__,
            }
        finally:
            if auth_client is not None:
                try:
                    auth_client.close()
                except Exception:
                    pass

    result = _map_result(row)
    slim = _slim_checkpoint(row, result=result, index=max(0, idx), prior=prior_cp)
    slim["PG_UNLOCK_RECOVERY_AT"] = _utc()
    slim["recovery_pass"] = "MICRO_DURING_FIRST_PASS"
    slim["PG_UNLOCK_ELIGIBLE"] = True
    slim["purchasing_group_id"] = (
        slim.get("purchasing_group_id") or pg_unlocked or row.get("purchasing_group_id")
    )
    slim["shared_access_key"] = (
        slim.get("shared_access_key")
        or item.get("shared_access_key")
        or (f"pg:{pg_unlocked}" if pg_unlocked else None)
    )
    store.write_item_checkpoint(job_id, slim)
    _append_rfq(row)

    # Remove from deferred on durable success; otherwise keep unlock-eligible.
    if result in _PRESERVE_RESULTS | {"SUCCESS", "PARTIAL"}:
        dq2 = _load_deferred_queue()
        dq2["items"] = [
            x for x in (dq2.get("items") or []) if str(x.get("opportunity_id")) != oid
        ]
        dq2["count"] = len(dq2["items"])
        dq2["pg_unlock_eligible_count"] = sum(
            1 for x in dq2["items"] if isinstance(x, dict) and x.get("recovery_eligible")
        )
        _save(DEFERRED_QUEUE_FILE, dq2)
    elif row.get("defer") or result in {"DEFERRED_ERROR", "DEFERRED_EXTERNAL", "TIMEOUT"}:
        still_unlock = bool(picked.get("recovery_eligible"))
        try:
            from bidnet_auth.purchasing_group_tracker import is_registered

            if pg_unlocked and is_registered(pg_unlocked):
                still_unlock = True
        except Exception:
            pass
        blocker = str(row.get("terminal_blocker") or "")
        if blocker.startswith("PAID_ENTITLEMENT"):
            still_unlock = False
        _defer_opportunity(
            opportunity_id=oid,
            stage=row.get("stage_reached") or "ACCESS_DOCS",
            outcome=str(row.get("funnel_outcome") or result),
            defect_key=row.get("software_defect_key"),
            shared_access_key=row.get("shared_access_key") or picked.get("shared_access_key"),
            blocker=row.get("terminal_blocker"),
            index=idx,
            recovery_eligible=still_unlock or bool(row.get("commercial_resume")),
            pg_unlocked=pg_unlocked if still_unlock else None,
            next_stage=row.get("next_stage"),
            last_completed_stage=row.get("last_completed_stage"),
            commercial_resume=bool(row.get("commercial_resume")),
        )

    out = {
        "processed": 1,
        "opportunity_id": oid,
        "purchasing_group_id": pg_unlocked or slim.get("purchasing_group_id"),
        "result": result,
        "stage_reached": slim.get("stage_reached"),
        "documents_downloaded": slim.get("documents_downloaded"),
        "fully_specified_product_lines": slim.get("fully_specified_product_lines"),
        "public_price_evidence_count": slim.get("public_price_evidence_count"),
        "supplier_status": slim.get("supplier_status"),
        "econ_state": slim.get("econ_state"),
        "terminal_blocker": slim.get("terminal_blocker"),
        "funnel_outcome": slim.get("funnel_outcome"),
        "stages_run": row.get("stages_run"),
        "at": _utc(),
    }
    try:
        job = store.load_job(job_id) or {}
        meta = dict(job.get("META") or {})
        hist = list(meta.get("unlock_micro_recovery_history") or [])
        hist.append(out)
        meta["unlock_micro_recovery_history"] = hist[-20:]
        meta["last_unlock_micro_recovery"] = out
        job["META"] = meta
        store.save_job(job)
    except Exception:
        pass
    return out


def validate_unlock_recovery_path(
    *,
    job_id: str | None = None,
    opportunity_id: str | None = None,
    browser_timeout_s: float = 45.0,
) -> dict[str, Any]:
    """Bounded production-path validation of PG-unlock recovery handoff.

    Uses the same orchestrator as production micro-recovery. Does NOT:
    - modify RESUME_CURSOR
    - start a competing production worker
    - acquire the FSPF job lease
    - drain the unlock queue
    """
    from bidnet_auth.browser_mutex_v1 import bidnet_browser_slot
    from buyer_intelligence.commercial_evidence_index_v1 import build_commercial_evidence_index
    from buyer_intelligence.commercial_funnel_execution_v1 import (
        _cookies_client,
        _open_bidnet_auth_client,
    )
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import run_one_opportunity
    from phase_l.l23_full_population_funnel import load_store

    store = JobStore(FAMILY)
    if not job_id:
        active = store.find_active_job(stale_seconds=86400)
        job_id = str((active or {}).get("JOB_ID") or "")
    if not job_id or not store.load_job(job_id):
        return {"ok": False, "error": "job_not_found", "build": BUILD}

    cursor_before = int((store.load_job(job_id) or {}).get("RESUME_CURSOR") or 0)
    queue = _load(QUEUE_FILE) or {}
    rows = queue.get("rows") or []
    dq = _load_deferred_queue()
    candidates = [
        d
        for d in (dq.get("items") or [])
        if isinstance(d, dict) and d.get("recovery_eligible") and d.get("opportunity_id")
    ]
    if opportunity_id:
        candidates = [d for d in candidates if str(d.get("opportunity_id")) == str(opportunity_id)]
    if not candidates:
        return {
            "ok": False,
            "error": "no_recovery_eligible_candidate",
            "build": BUILD,
            "job_id": job_id,
            "resume_cursor_before": cursor_before,
            "resume_cursor_after": cursor_before,
        }

    picked = candidates[0]
    oid = str(picked.get("opportunity_id"))
    idx = int(picked.get("queue_index") or -1)
    item = rows[idx] if 0 <= idx < len(rows) else {"opportunity_id": oid}
    if str(item.get("opportunity_id") or "") != oid:
        item = {"opportunity_id": oid}
        for j, r in enumerate(rows):
            if str(r.get("opportunity_id")) == oid:
                item = r
                idx = j
                break
    prior_cp = store.load_item(job_id, oid) or {}
    pg_unlocked = str(picked.get("pg_unlocked") or prior_cp.get("purchasing_group_id") or "").strip() or None
    item = dict(item)
    item["opportunity_id"] = oid
    item["authoritative_url"] = (
        item.get("authoritative_url")
        or prior_cp.get("authoritative_url")
        or prior_cp.get("AUTHORITATIVE_URL")
    )
    item["title"] = item.get("title") or prior_cp.get("title")
    item["buyer"] = item.get("buyer") or prior_cp.get("buyer")
    item["purchasing_group_id"] = pg_unlocked or item.get("purchasing_group_id")
    item["shared_access_key"] = (
        picked.get("shared_access_key")
        or (f"pg:{pg_unlocked}" if pg_unlocked else None)
    )
    item["stage_reached"] = picked.get("stage_reached") or prior_cp.get("stage_reached") or "ACCESS_DOCS"
    item["recovery_eligible"] = True
    item["_prior_checkpoint"] = prior_cp
    item["commercial_resume"] = bool(
        picked.get("commercial_resume") or prior_cp.get("commercial_resume")
    )
    item["next_stage"] = (
        picked.get("next_stage") or prior_cp.get("next_stage") or item.get("stage_reached")
    )
    item["last_completed_stage"] = picked.get("last_completed_stage") or prior_cp.get(
        "last_completed_stage"
    )
    if prior_cp.get("line_items"):
        item["line_items"] = prior_cp.get("line_items")
    if prior_cp.get("recovered_documents"):
        item["recovered_documents"] = prior_cp.get("recovered_documents")
    if prior_cp.get("complete_spec"):
        item["complete_spec"] = True
    if prior_cp.get("documents_downloaded"):
        item["documents_downloaded"] = prior_cp.get("documents_downloaded")

    # Confirm registered access without inventing membership.
    pg_registered = False
    try:
        from bidnet_auth.purchasing_group_tracker import is_registered

        pg_registered = bool(pg_unlocked and is_registered(pg_unlocked))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"pg_status_check_failed:{type(exc).__name__}",
            "build": BUILD,
            "opportunity_id": oid,
            "purchasing_group_id": pg_unlocked,
        }
    if not pg_registered:
        return {
            "ok": False,
            "error": "purchasing_group_not_registered",
            "build": BUILD,
            "opportunity_id": oid,
            "purchasing_group_id": pg_unlocked,
            "resume_cursor_before": cursor_before,
            "resume_cursor_after": int((store.load_job(job_id) or {}).get("RESUME_CURSOR") or 0),
        }

    evidence_index = build_commercial_evidence_index()
    l23 = load_store()
    http = _cookies_client()
    auth_client = None
    row: dict[str, Any] = {}
    browser_got = False
    try:
        with bidnet_browser_slot(
            holder=f"validate-unlock-{uuid.uuid4().hex[:8]}",
            timeout_s=float(browser_timeout_s),
            ttl_s=240.0,
        ) as got:
            browser_got = bool(got)
            if not got:
                return {
                    "ok": False,
                    "error": "browser_slot_busy",
                    "build": BUILD,
                    "opportunity_id": oid,
                    "purchasing_group_id": pg_unlocked,
                    "note": "first_pass_or_pg_manager_holds_playwright",
                    "resume_cursor_before": cursor_before,
                    "resume_cursor_after": int(
                        (store.load_job(job_id) or {}).get("RESUME_CURSOR") or 0
                    ),
                }
            auth_status, auth_client = _open_bidnet_auth_client()
            if not auth_status.get("authenticated") or auth_client is None:
                return {
                    "ok": False,
                    "error": "auth_unavailable",
                    "build": BUILD,
                    "opportunity_id": oid,
                    "purchasing_group_id": pg_unlocked,
                    "auth_status": auth_status.get("status"),
                    "auth_message": (auth_status.get("message") or "")[:200],
                    "resume_cursor_before": cursor_before,
                    "resume_cursor_after": int(
                        (store.load_job(job_id) or {}).get("RESUME_CURSOR") or 0
                    ),
                }
            row = run_one_opportunity(
                item,
                client=http,
                evidence_index=evidence_index,
                auth_status=auth_status,
                l23_rec=(l23 or {}).get(oid) or {},
                auth_client=auth_client,
            )
    except Exception as exc:
        row = {
            "opportunity_id": oid,
            "terminal_blocker": type(exc).__name__,
            "funnel_outcome": "SOFTWARE_ERROR",
            "stage_reached": item.get("stage_reached") or "ACCESS_DOCS",
            "defer": True,
            "software_defect_key": type(exc).__name__,
            "stages_run": ["QUALIFY", "ACCESS_DOCS"],
        }
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass
        try:
            http.close()
        except Exception:
            pass

    result = _map_result(row)
    slim = _slim_checkpoint(row, result=result, index=max(0, idx), prior=prior_cp)
    slim["PG_UNLOCK_RECOVERY_AT"] = _utc()
    slim["recovery_pass"] = "VALIDATE_UNLOCK_HANDOFF"
    slim["PG_UNLOCK_ELIGIBLE"] = True
    slim["purchasing_group_id"] = slim.get("purchasing_group_id") or pg_unlocked
    slim["shared_access_key"] = slim.get("shared_access_key") or item.get("shared_access_key")
    store.write_item_checkpoint(job_id, slim)
    _append_rfq(row)

    # Keep in unlock recovery set unless successfully completed or paid entitlement.
    if result in _PRESERVE_RESULTS | {"SUCCESS", "PARTIAL"}:
        dq2 = _load_deferred_queue()
        dq2["items"] = [
            x for x in (dq2.get("items") or []) if str(x.get("opportunity_id")) != oid
        ]
        dq2["count"] = len(dq2["items"])
        dq2["pg_unlock_eligible_count"] = sum(
            1 for x in dq2["items"] if isinstance(x, dict) and x.get("recovery_eligible")
        )
        _save(DEFERRED_QUEUE_FILE, dq2)
        still_in_recovery = False
    else:
        still_unlock = True
        blocker = str(row.get("terminal_blocker") or "")
        if blocker.startswith("PAID_ENTITLEMENT"):
            still_unlock = False
        _defer_opportunity(
            opportunity_id=oid,
            stage=row.get("stage_reached") or "ACCESS_DOCS",
            outcome=str(row.get("funnel_outcome") or result),
            defect_key=row.get("software_defect_key"),
            shared_access_key=row.get("shared_access_key") or item.get("shared_access_key"),
            blocker=row.get("terminal_blocker"),
            index=idx,
            recovery_eligible=still_unlock or bool(row.get("commercial_resume")),
            pg_unlocked=pg_unlocked if still_unlock else None,
            next_stage=row.get("next_stage"),
            last_completed_stage=row.get("last_completed_stage"),
            commercial_resume=bool(row.get("commercial_resume")),
        )
        still_in_recovery = still_unlock or bool(row.get("commercial_resume"))

    cursor_after = int((store.load_job(job_id) or {}).get("RESUME_CURSOR") or 0)
    recon = reconcile_released_pg_unlocks(job_id, repair=False)
    stages = list(row.get("stages_run") or [])
    docs = int(slim.get("documents_downloaded") or 0)
    lines = int(slim.get("fully_specified_product_lines") or 0)
    price_n = int(slim.get("public_price_evidence_count") or 0)
    # Handoff PASS: selected unlock ID, registered PG, orchestrator ran past qualify,
    # and either advanced commercially or recorded a precise external/software blocker.
    handoff_pass = bool(
        browser_got
        and pg_registered
        and oid
        and cursor_before == cursor_after
        and (
            docs > 0
            or lines > 0
            or price_n > 0
            or "ACCESS_DOCS" in stages
            or "EXTRACT" in stages
            or slim.get("terminal_blocker")
            or slim.get("funnel_outcome")
        )
    )
    return {
        "ok": True,
        "build": BUILD,
        "job_id": job_id,
        "handoff": "PASS" if handoff_pass else "FAIL",
        "opportunity_id": oid,
        "purchasing_group_id": pg_unlocked,
        "pg_registered": pg_registered,
        "shared_access_key": item.get("shared_access_key"),
        "authoritative_url": (item.get("authoritative_url") or "")[:220] or None,
        "result": result,
        "stage_reached": slim.get("stage_reached"),
        "stages_run": stages,
        "documents_downloaded": docs,
        "fully_specified_product_lines": lines,
        "supplier_status": slim.get("supplier_status"),
        "public_price_evidence_count": price_n,
        "econ_state": slim.get("econ_state"),
        "terminal_blocker": slim.get("terminal_blocker"),
        "funnel_outcome": slim.get("funnel_outcome") or row.get("funnel_outcome"),
        "fetch_via": slim.get("fetch_via") or row.get("fetch_via"),
        "still_in_recovery_queue": still_in_recovery,
        "resume_cursor_before": cursor_before,
        "resume_cursor_after": cursor_after,
        "cursor_unchanged": cursor_before == cursor_after,
        "reconcile": {
            "released_unique": recon.get("released_opportunity_ids_count"),
            "fully_accounted": recon.get("fully_accounted"),
            "orphans": recon.get("missing_or_orphaned"),
            "recovery_eligible_in_deferred": recon.get("recovery_eligible_in_deferred"),
            "summary": recon.get("summary"),
        },
        "at": _utc(),
    }


def requeue_retryable_failures(job_id: str) -> dict[str, Any]:
    """Mark systemic technical failures PENDING_RETRY without wiping successes.

    Preserves SUCCESS / good PARTIAL / expired / owner-action checkpoints.
    Bounded by MAX_TECHNICAL_RETRIES per opportunity.
    """
    store = JobStore(FAMILY)
    job = store.load_job(job_id)
    if not job:
        raise KeyError(job_id)
    # First: unlock any opportunities whose purchasing group is now registered
    pg_release = {}
    try:
        pg_release = release_pg_blocked_for_registered_groups(job_id)
    except Exception as exc:
        pg_release = {"error": type(exc).__name__}
    items = store.list_items(job_id)
    marked = 0
    preserved = 0
    exhausted = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        oid = str(it.get("OPPORTUNITY_ID") or "")
        result = str(it.get("RESULT") or "")
        blocker = str(it.get("terminal_blocker") or "")
        retries = int(it.get("RETRY_COUNT") or 0)
        fully = int(it.get("fully_specified_product_lines") or 0)
        docs = int(it.get("documents_downloaded") or 0)

        if result in _PRESERVE_RESULTS:
            preserved += 1
            continue
        if result == "PARTIAL" and fully > 0 and docs > 0:
            preserved += 1
            continue
        if result == "PENDING_RETRY" and retries >= MAX_TECHNICAL_RETRIES:
            exhausted += 1
            continue

        technical = (
            result in {"TIMEOUT", "FETCH_FAILED", "NO_PACKAGE", "PARSE_FAILED", "AUTH_FAILED", "PARTIAL"}
            or blocker in _RETRYABLE_BLOCKERS
            or blocker.startswith("ABSTRACT_")
            or blocker.startswith("DETAIL_")
            or blocker.startswith("WAF_")
            or blocker.startswith("MISSING_")
            or blocker.startswith("PER_OPPORTUNITY_TIMEOUT")
            or blocker.startswith("PROCESS_EXCEPTION")
        )
        if not technical:
            preserved += 1
            continue
        # Extra retry budgets after production path repairs (statewide→private unlock)
        if retries >= MAX_TECHNICAL_RETRIES:
            if (
                blocker in {"DETAIL_FETCH_EMPTY", "ABSTRACT_FETCH_FAILED_OR_WAF"}
                and not it.get("BROWSER_PATH_REPAIR_RETRY")
            ):
                it["BROWSER_PATH_REPAIR_RETRY"] = True
                it["RETRY_COUNT"] = 0
                retries = 0
            elif (
                blocker == "DETAIL_FETCH_EMPTY"
                and (
                    it.get("purchasing_group_registration_status") == "REGISTERED"
                    or it.get("pg_registered_awaiting_docs")
                )
                and not it.get("REGISTERED_UNLOCK_REPAIR_RETRY")
            ):
                it["REGISTERED_UNLOCK_REPAIR_RETRY"] = True
                it["RETRY_COUNT"] = 0
                retries = 0
            else:
                exhausted += 1
                continue

        it["PREVIOUS_RESULT"] = result
        it["PREVIOUS_BLOCKER"] = blocker
        it["RETRYABLE"] = True
        it["RESULT"] = "PENDING_RETRY"
        it["RETRY_QUEUED_AT"] = _utc()
        it["NOTE"] = "Requeued after production funnel repair — prior checkpoint preserved in PREVIOUS_*"
        store.write_item_checkpoint(job_id, it)
        marked += 1

    # Resume from earliest pending index
    job = store.load_job(job_id) or job
    job["RESUME_CURSOR"] = 0
    job["STATUS"] = "INTERRUPTED"
    job["META"] = {
        **(job.get("META") or {}),
        "retryable_requeued": marked,
        "retryable_preserved": preserved,
        "retryable_exhausted": exhausted,
        "repair_build": BUILD,
    }
    store.save_job(job)
    store.recompute_counts(job_id)
    return {
        "job_id": job_id,
        "marked_pending_retry": marked,
        "preserved": preserved,
        "retry_budget_exhausted": exhausted,
        "max_technical_retries": MAX_TECHNICAL_RETRIES,
        "pg_release": pg_release,
        "build": BUILD,
    }


def _is_active_lease_worker(
    job_id: str,
    worker_id: str,
    *,
    lease_generation: int | None = None,
) -> bool:
    """True only while this worker still holds the exclusive job lease.

    Prevents zombie threads (from force-restart) from continuing after a newer
    worker acquires the lease — clearing _stop_requested must not revive them.
    Generation fencing blocks superseded workers even if WORKER_ID were reused.
    """
    try:
        job = JobStore(FAMILY).load_job(job_id) or {}
    except Exception:
        return False
    if str(job.get("WORKER_ID") or "") != str(worker_id or ""):
        return False
    if lease_generation is not None:
        if int(job.get("LEASE_GENERATION") or 0) != int(lease_generation):
            return False
    return str(job.get("STATUS") or "") in {"RUNNING", "CREATED"}


def _run_with_timeout_no_hang(
    fn: Any,
    *,
    timeout_s: float,
    on_timeout: Any = None,
) -> Any:
    """Run ``fn`` with a wall-clock timeout WITHOUT blocking forever on shutdown.

    Python's ``with ThreadPoolExecutor`` waits for the worker on exit. A hung
    Playwright call that already exceeded ``timeout_s`` would then freeze the
    entire FSPF worker (no heartbeats) for hours. Always shutdown(wait=False).
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout_s)
        except FuturesTimeout:
            if callable(on_timeout):
                return on_timeout()
            raise
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 cancel_futures unavailable
            pool.shutdown(wait=False)


def _heartbeat_pulse(
    *,
    job_id: str,
    worker_id: str,
    lease_generation: int | None,
    current_index: int | None = None,
    current_opportunity_id: str | None = None,
    stop_event: threading.Event,
    interval_s: float = WORKER_HEARTBEAT_S,
) -> None:
    """Independent heartbeat while a long opportunity op runs."""
    store = JobStore(FAMILY)
    while not stop_event.wait(interval_s):
        if not _is_active_lease_worker(
            job_id, worker_id, lease_generation=lease_generation
        ):
            break
        try:
            store.heartbeat(
                job_id,
                worker_id=worker_id,
                current_index=current_index,
                current_opportunity_id=current_opportunity_id,
                lease_generation=lease_generation,
            )
            _progress(
                status="RUNNING",
                phase="WORKER_HEARTBEAT",
                job_id=job_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
                current_index=current_index,
                current_opportunity_id=current_opportunity_id,
            )
        except JobLeaseError:
            break
        except Exception:
            break


def _stop_flag() -> bool:
    if _stop_requested:
        return True
    blob = _load(STOP_FILE) or {}
    return bool(blob.get("stop"))


def _priority_score(row: dict[str, Any]) -> float:
    score = float(row.get("research_priority_score") or 0)
    title = str(row.get("product") or row.get("title") or "")
    if row.get("common_product") or _COMMON.search(title):
        score += 20
    if _COMPLEX.search(title):
        score -= 40
    try:
        dleft = float(row.get("days_left")) if row.get("days_left") is not None else 999.0
    except Exception:
        dleft = 999.0
    if 2 <= dleft <= 21:
        score += 25
    elif 21 < dleft <= 45:
        score += 12
    elif dleft < 2:
        score -= 30
    url = str(row.get("authoritative_url") or "").lower()
    if "bidnet" in url:
        score += 8
    if row.get("product_class") == "PRODUCT":
        score += 10
    return score


def _save_queue_compact(man: dict[str, Any]) -> Path:
    """Compact JSON (no indent) — large queues must stay writable under memory pressure."""
    path = Path(data_path(QUEUE_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(man, separators=(",", ":"), default=str)
    try:
        import tempfile

        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except OSError:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
            path.write_text(raw, encoding="utf-8")
    except Exception:
        path.write_text(raw, encoding="utf-8")
    return path


def _queue_row_from_any(r: dict[str, Any], *, oid: str | None = None) -> dict[str, Any] | None:
    oid = str(oid or r.get("opportunity_id") or r.get("canonical_opportunity_id") or "").strip()
    if not oid:
        return None
    title = str(r.get("product") or r.get("title") or "")
    # Do NOT drop complex titles from the universe — fast_qualify handles service
    # rejection with evidence. Demote priority only.
    complex_title_needs_qualify = bool(_COMPLEX.search(title) and not _COMMON.search(title))
    pc = str(r.get("product_class") or r.get("product_service_classification") or "")
    if pc in {"NON_PRODUCT", "PURE_SERVICE", "CONSTRUCTION"}:
        return None
    if not pc:
        pc = "UNKNOWN_PRODUCTNESS"
    elif "PRODUCT" in pc.upper() or pc in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS", "TANGIBLE_PRODUCT"}:
        if pc == "TANGIBLE_PRODUCT":
            pc = "PRODUCT"
        elif pc == "MIXED_PRODUCT_SERVICE":
            pc = "MIXED"
    else:
        pc = "UNKNOWN_PRODUCTNESS"
    dl_status = str(r.get("deadline_status") or "")
    if dl_status in {"CLOSED_CONFIRMED", "CANCELLED"}:
        return None
    # days_left if present
    days = r.get("days_left")
    if days is None and r.get("deadline"):
        try:
            from dateutil import parser as dp

            d = dp.isoparse(str(r.get("deadline")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            days = (now_utc() - d).total_seconds() / -86400.0
        except Exception:
            days = None
    if days is not None:
        try:
            if float(days) < 0:
                return None
        except Exception:
            pass
    fun = str(r.get("funnel_class") or r.get("visibility_bucket") or "LIVE_ELIGIBLE")
    if fun in {"EXPIRED", "EXPIRED_HISTORICAL", "CANCELLED", "BLOCKED_INELIGIBLE", "SET_ASIDE_INELIGIBLE"}:
        return None
    row = {
        "opportunity_id": oid,
        "title": title[:240],
        "buyer": r.get("buyer") or r.get("agency") or r.get("organization"),
        "deadline": r.get("deadline"),
        "days_left": days,
        "authoritative_url": r.get("authoritative_url") or r.get("url") or r.get("source_url"),
        "product_class": pc if pc in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"} else "UNKNOWN_PRODUCTNESS",
        "funnel_class": "LIVE_ELIGIBLE",
        "deadline_status": dl_status or ("OPEN_CONFIRMED" if days is not None else "DEADLINE_UNKNOWN"),
        "common_product": bool(r.get("common_product") or _COMMON.search(title)),
        "complex_title_needs_qualify": complex_title_needs_qualify,
        "selection_score": 0.0,
    }
    row["selection_score"] = _priority_score(row)
    if complex_title_needs_qualify:
        row["selection_score"] = float(row["selection_score"] or 0) - 25.0
    return row


def build_eligible_queue(
    *,
    force_rebuild: bool = False,
    job_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Build prioritized full LIVE_ELIGIBLE product queue.

    Prefer FCF classifications, then Available Deals artifacts, then a fast L23
    heuristic pass (same gates, no per-row heavy classify). No artificial total cap.
    """
    existing = None if force_rebuild else _load(QUEUE_FILE)
    if (
        existing
        and isinstance(existing.get("rows"), list)
        and existing.get("eligible_total", 0) > 0
        and not force_rebuild
    ):
        return existing

    def _hb(examined: int, found: int, phase: str) -> None:
        _progress(
            status="RUNNING",
            phase=phase,
            job_id=job_id,
            worker_id=worker_id,
            queue_examined=examined,
            queue_found=found,
        )
        if job_id and worker_id:
            try:
                JobStore(FAMILY).heartbeat(job_id, worker_id=worker_id)
            except Exception:
                pass

    rows_in: list[dict[str, Any]] = []
    source = "none"

    # 1) FCF classifications (full audit evidence)
    cls = _load(FCF_CLASS) or {}
    if isinstance(cls.get("rows"), list) and cls["rows"]:
        rows_in = [r for r in cls["rows"] if isinstance(r, dict)]
        source = "fcf_classifications"
        _hb(len(rows_in), 0, "QUEUE_FROM_FCF")

    # 2) Available Deals visibility artifact
    # Prefer full funnel admission (worth + ready + needs_verification). Older ADV
    # artifacts stored needs separately / truncated — merge them so missing deadlines
    # are not silently excluded from the production queue.
    if not rows_in:
        for rel in (
            f"{OUT}/m3_adv_v1_available_deals.json",
            f"{OUT}/m3_available_deals_visibility_v1.json",
            f"{OUT}/m3_fcf_v1_top20_actionable.json",
        ):
            blob = _load(rel) or {}
            deals = blob.get("deals") or blob.get("rows") or blob.get("opportunities") or []
            if isinstance(deals, dict):
                deals = list(deals.values())
            needs = blob.get("needs_verification_deals") or []
            if isinstance(needs, dict):
                needs = list(needs.values())
            merged: list[dict[str, Any]] = []
            seen_oids: set[str] = set()
            for d in list(deals or []) + list(needs or []):
                if not isinstance(d, dict):
                    continue
                oid = str(d.get("opportunity_id") or d.get("OPPORTUNITY_ID") or "").strip()
                if oid and oid in seen_oids:
                    continue
                if oid:
                    seen_oids.add(oid)
                merged.append(d)
            if merged:
                rows_in = merged
                source = f"available_deals:{rel}"
                _hb(len(rows_in), 0, "QUEUE_FROM_ADV")
                break

    # 3) Fast L23 heuristic (full population — no pilot cap)
    if not rows_in:
        from phase_l.l23_full_population_funnel import load_store

        _hb(0, 0, "QUEUE_LOAD_L23")
        store = load_store()
        rebuilt: list[dict[str, Any]] = []
        for i, (oid, rec) in enumerate((store or {}).items()):
            if not isinstance(rec, dict):
                continue
            # Lightweight open/product gate — expensive research happens later per item
            if str(rec.get("current_funnel_state") or "").upper() in {
                "EXPIRED",
                "CANCELED",
                "CANCELLED",
            }:
                continue
            title = str(rec.get("title") or "")
            row = _queue_row_from_any(
                {
                    **rec,
                    "product": title,
                    "product_class": rec.get("product_service_classification") or "UNKNOWN_PRODUCTNESS",
                    "funnel_class": "LIVE_ELIGIBLE",
                },
                oid=str(oid),
            )
            if row:
                rebuilt.append(row)
            if (i + 1) % 500 == 0:
                _hb(i + 1, len(rebuilt), "QUEUE_FROM_L23")
        rows_in = rebuilt
        source = "l23_fast_heuristic"
        log.warning("FCF/ADV missing — built eligible queue from L23 fast path (%s)", len(rows_in))
        _hb(len(store or {}), len(rows_in), "QUEUE_L23_DONE")

    selected: list[dict[str, Any]] = []
    for i, r in enumerate(rows_in):
        if not isinstance(r, dict):
            continue
        # FCF rows already classified
        if source == "fcf_classifications":
            if r.get("funnel_class") != "LIVE_ELIGIBLE":
                continue
            if r.get("product_class") not in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
                continue
            if r.get("deadline_status") in {"CLOSED_CONFIRMED", "CANCELLED"}:
                continue
            row = _queue_row_from_any(r)
        else:
            # ADV may use visibility buckets
            bucket = str(r.get("visibility_bucket") or r.get("bucket") or "")
            if bucket in {"BLOCKED_INELIGIBLE", "BLOCKED"}:
                continue
            row = _queue_row_from_any(r)
        if row:
            selected.append(row)
        if (i + 1) % 1000 == 0:
            _hb(i + 1, len(selected), "QUEUE_FILTER")

    selected.sort(
        key=lambda x: (
            0 if x.get("common_product") else 1,
            0 if "bidnet" in str(x.get("authoritative_url") or "").lower() else 1,
            -(x.get("selection_score") or 0),
            str(x.get("opportunity_id") or ""),
        )
    )

    fcf_metrics = (_load(FCF_SCORE) or {}).get("metrics") or {}
    man = {
        "kind": "FullScaleProductionFunnelEligibleQueue",
        "build": BUILD,
        "created_at": _utc(),
        "source": source,
        "AUTO_SEND": AUTO_SEND,
        "eligible_total": len(selected),
        "fcf_eligible_product_reported": fcf_metrics.get("eligible_product_opportunities"),
        "NOTE": "Full eligible queue — no pilot cap. Slices are internal only.",
        "rows": selected,
    }
    _save_queue_compact(man)
    _hb(len(selected), len(selected), "QUEUE_SAVED")
    return man


def archive_prior_run(job_id: str) -> dict[str, Any]:
    """Archive checkpoint/scorecard/progress for a completed/stopped run. Preserves all evidence."""
    store = JobStore(FAMILY)
    job = store.load_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found", "job_id": job_id}
    arch_root = Path(data_path(ARCHIVE_DIR)) / job_id
    arch_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "archived_at": _utc(),
        "build": BUILD,
        "job": job,
        "progress": _load(PROGRESS_FILE),
        "scorecard": _load(SCORECARD_FILE),
        "queue_meta": {
            k: v
            for k, v in (_load(QUEUE_FILE) or {}).items()
            if k != "rows"
        },
        "queue_eligible_total": (_load(QUEUE_FILE) or {}).get("eligible_total"),
        "item_count": len(store.list_items(job_id)),
        "NOTE": "Items remain under jobs/<family>/<job_id>/items — not deleted",
    }
    (arch_root / "archive_manifest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    # Mark job archived (not deleted)
    job["STATUS"] = "ARCHIVED"
    job["FINISHED_AT"] = job.get("FINISHED_AT") or _utc()
    job["META"] = {**(job.get("META") or {}), "archived_at": _utc(), "archive_path": str(arch_root)}
    store.save_job(job)
    # Clear active pointer if it points here
    try:
        ptr = store.active_pointer_path()
        if ptr.exists():
            cur = json.loads(ptr.read_text(encoding="utf-8"))
            if str(cur.get("JOB_ID")) == job_id:
                ptr.unlink()
    except Exception:
        pass
    return {
        "ok": True,
        "job_id": job_id,
        "archive_path": str(arch_root),
        "item_count": payload["item_count"],
        "eligible_total": payload["queue_eligible_total"],
    }


def verify_expanded_queue_integrity(
    *,
    expected_total: int | None = None,
    expected_original: int = 10099,
    expected_appended: int | None = None,
) -> dict[str, Any]:
    """Bounded uniqueness / durability check for the expanded production queue.

    When expected_total is omitted, the live queue ``eligible_total`` / unique
    count is the authority (supports further L23 admissions beyond 29,845).
    """
    queue = _load(QUEUE_FILE) or {}
    rows = [r for r in (queue.get("rows") or []) if isinstance(r, dict)]
    ids = [str(r.get("opportunity_id") or "").strip() for r in rows]
    ids = [x for x in ids if x]
    unique = set(ids)
    appended_flag = [
        str(r.get("opportunity_id") or "").strip()
        for r in rows
        if isinstance(r, dict) and r.get("l23_universe_append") and r.get("opportunity_id")
    ]
    append_man = _load(f"{OUT}/m3_l23_universe_append_manifest_v1.json") or {}
    sample_appended = list(append_man.get("sample_appended_ids") or [])
    missing_samples = [x for x in sample_appended if x not in unique]
    live_total = int(queue.get("eligible_total") or len(unique) or 0)
    if expected_total is None:
        expected_total = live_total
    if expected_appended is None:
        expected_appended = max(0, live_total - int(expected_original))
    store = JobStore(FAMILY)
    active = store.find_active_job(stale_seconds=86400) or {}
    job_id = str(active.get("JOB_ID") or "")
    job = store.load_job(job_id) if job_id else None
    cursor = int((job or {}).get("RESUME_CURSOR") or 0) if job else None
    return {
        "kind": "ExpandedQueueIntegrity",
        "build": BUILD,
        "at": _utc(),
        "expected_total": expected_total,
        "expected_original": expected_original,
        "expected_appended": expected_appended,
        "baseline_29845_preserved": len(unique) >= 29845,
        "row_count": len(rows),
        "id_count": len(ids),
        "unique_ids": len(unique),
        "duplicate_ids": len(ids) - len(unique),
        "eligible_total_field": queue.get("eligible_total"),
        "l23_append_flag_count": len(appended_flag),
        "append_manifest_appended": append_man.get("appended"),
        "append_manifest_queue_after": append_man.get("queue_after"),
        "sample_appended_present": len(sample_appended) - len(missing_samples),
        "sample_appended_missing": missing_samples[:20],
        "resume_cursor": cursor,
        "job_id": job_id or None,
        "job_status": (job or {}).get("STATUS"),
        "ok": bool(
            len(unique) == expected_total
            and len(ids) == expected_total
            and (len(ids) - len(unique)) == 0
            and int(queue.get("eligible_total") or 0) == expected_total
            and not missing_samples
            and len(unique) >= 29845
        ),
    }


def reconcile_eligible_universe() -> dict[str, Any]:
    """Explain ADV 10,099 vs production queue 9,859 — account for every excluded ID."""
    from phase_l.l23_full_population_funnel import load_store

    fcf = _load(FCF_CLASS) or {}
    fcf_rows = [r for r in (fcf.get("rows") or []) if isinstance(r, dict)]
    fcf_live = [
        r
        for r in fcf_rows
        if r.get("funnel_class") == "LIVE_ELIGIBLE"
        and r.get("product_class") in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}
        and r.get("deadline_status") not in {"CLOSED_CONFIRMED", "CANCELLED"}
    ]
    fcf_score = (_load(FCF_SCORE) or {}).get("metrics") or {}

    adv = _load(f"{OUT}/m3_adv_v1_available_deals.json") or {}
    adv_deals = adv.get("deals") or adv.get("rows") or []
    if isinstance(adv_deals, dict):
        adv_deals = list(adv_deals.values())
    adv_deals = [d for d in adv_deals if isinstance(d, dict)]
    adv_blocked = sum(
        1
        for d in adv_deals
        if str(d.get("visibility_bucket") or d.get("bucket") or "")
        in {"BLOCKED_INELIGIBLE", "BLOCKED"}
    )

    store = load_store() or {}
    l23_total = len(store)
    l23_expired = sum(
        1
        for r in store.values()
        if isinstance(r, dict)
        and str(r.get("current_funnel_state") or "").upper() in {"EXPIRED", "CANCELED", "CANCELLED"}
    )

    fcf_ids = {str(r.get("opportunity_id") or "") for r in fcf_live if r.get("opportunity_id")}
    adv_actionable_rows = [
        d
        for d in adv_deals
        if (d.get("opportunity_id") or d.get("OPPORTUNITY_ID"))
        and str(d.get("visibility_bucket") or d.get("bucket") or "")
        not in {"BLOCKED_INELIGIBLE", "BLOCKED"}
    ]
    adv_ids = {
        str(d.get("opportunity_id") or d.get("OPPORTUNITY_ID") or "")
        for d in adv_actionable_rows
    }
    only_fcf = sorted(fcf_ids - adv_ids)
    only_adv = sorted(adv_ids - fcf_ids)
    overlap = len(fcf_ids & adv_ids)

    # Production queue vs ADV actionable — explain the ~240 gap by exclusion reason
    queue = _load(QUEUE_FILE) or {}
    queue_rows = [r for r in (queue.get("rows") or []) if isinstance(r, dict)]
    queue_ids = {
        str(r.get("opportunity_id") or "") for r in queue_rows if r.get("opportunity_id")
    }
    in_adv_not_queue = sorted(adv_ids - queue_ids)
    in_queue_not_adv = sorted(queue_ids - adv_ids)
    exclusion_reasons: dict[str, list[str]] = {
        "missing_opportunity_id": [],
        "complex_non_common_title": [],
        "non_product_class": [],
        "closed_or_cancelled_deadline": [],
        "days_left_negative": [],
        "expired_funnel_class": [],
        "queue_row_rejected_other": [],
    }
    for d in adv_actionable_rows:
        oid = str(d.get("opportunity_id") or d.get("OPPORTUNITY_ID") or "").strip()
        if not oid or oid in queue_ids:
            continue
        title = str(d.get("product") or d.get("title") or "")
        pc = str(d.get("product_class") or d.get("product_service_classification") or "")
        dl = str(d.get("deadline_status") or "")
        fun = str(d.get("funnel_class") or d.get("visibility_bucket") or "")
        days = d.get("days_left")
        if not oid:
            exclusion_reasons["missing_opportunity_id"].append(oid or "?")
        elif _COMPLEX.search(title) and not _COMMON.search(title):
            exclusion_reasons["complex_non_common_title"].append(oid)
        elif pc in {"NON_PRODUCT", "PURE_SERVICE", "CONSTRUCTION"}:
            exclusion_reasons["non_product_class"].append(oid)
        elif dl in {"CLOSED_CONFIRMED", "CANCELLED"}:
            exclusion_reasons["closed_or_cancelled_deadline"].append(oid)
        elif days is not None:
            try:
                if float(days) < 0:
                    exclusion_reasons["days_left_negative"].append(oid)
                elif fun in {
                    "EXPIRED",
                    "EXPIRED_HISTORICAL",
                    "CANCELLED",
                    "BLOCKED_INELIGIBLE",
                    "SET_ASIDE_INELIGIBLE",
                }:
                    exclusion_reasons["expired_funnel_class"].append(oid)
                else:
                    exclusion_reasons["queue_row_rejected_other"].append(oid)
            except Exception:
                exclusion_reasons["queue_row_rejected_other"].append(oid)
        elif fun in {
            "EXPIRED",
            "EXPIRED_HISTORICAL",
            "CANCELLED",
            "BLOCKED_INELIGIBLE",
            "SET_ASIDE_INELIGIBLE",
        }:
            exclusion_reasons["expired_funnel_class"].append(oid)
        else:
            # Re-run gate to catch residual rejects
            if _queue_row_from_any(d) is None:
                exclusion_reasons["queue_row_rejected_other"].append(oid)

    reason_counts = {k: len(v) for k, v in exclusion_reasons.items()}
    accounted = sum(reason_counts.values())

    report = {
        "kind": "EligibleUniverseReconciliation",
        "build": BUILD,
        "at": _utc(),
        "frozen_cohort_historical": 28046,
        "fcf_rows_total": len(fcf_rows),
        "fcf_live_eligible_product": len(fcf_live),
        "fcf_scorecard_eligible_product": fcf_score.get("eligible_product_opportunities"),
        "adv_deals_total": len(adv_deals),
        "adv_blocked": adv_blocked,
        "adv_actionable": len(adv_ids),
        "production_queue_total": len(queue_ids),
        "adv_minus_queue": len(in_adv_not_queue),
        "queue_minus_adv": len(in_queue_not_adv),
        "exclusion_reason_counts": reason_counts,
        "exclusion_accounted": accounted,
        "exclusion_samples": {k: v[:10] for k, v in exclusion_reasons.items() if v},
        "sample_adv_not_in_queue": in_adv_not_queue[:25],
        "sample_queue_not_in_adv": in_queue_not_adv[:15],
        "l23_store_total": l23_total,
        "l23_expired_or_cancelled": l23_expired,
        "overlap_fcf_adv": overlap,
        "only_in_fcf_not_adv": len(only_fcf),
        "only_in_adv_not_fcf": len(only_adv),
        "difference_10725_minus_9859": (len(fcf_live) or int(fcf_score.get("eligible_product_opportunities") or 0))
        - len(adv_ids),
        "difference_10099_minus_9859": len(adv_ids) - len(queue_ids),
        "sample_only_fcf": only_fcf[:15],
        "sample_only_adv": only_adv[:15],
        "recommended_queue_source": (
            "fcf_classifications"
            if len(fcf_live) >= len(adv_ids)
            else "available_deals"
        ),
        "NOTE": (
            "Production queue applies product/deadline filters on ADV actionable rows "
            "(_COMPLEX title without common product terms, NON_PRODUCT classes, expired days_left). "
            "Queue is not silently rebuilt; exclusions are listed by ID reason."
        ),
    }
    _save(f"{OUT}/m3_fspf_v2_eligible_universe_reconciliation.json", report)
    # Persist full exclusion ID lists separately (compact)
    _save(
        f"{OUT}/m3_fspf_v2_adv_not_in_queue_exclusions.json",
        {
            "kind": "AdvNotInQueueExclusions",
            "build": BUILD,
            "at": _utc(),
            "adv_actionable": len(adv_ids),
            "queue_total": len(queue_ids),
            "excluded_total": len(in_adv_not_queue),
            "by_reason": exclusion_reasons,
        },
    )
    return report


def production_regression_copy_paper() -> dict[str, Any]:
    """Regression: shared BidNetAuthenticatedClient + schedule extraction (production modules)."""
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, upsert_group
    from bidnet_engine.schedule_extraction import extract_schedules_from_package
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import recover_one_bounded
    from m3_portal_access_map import PORTAL_BIDNET, bind_membership, seed_bidnet_defaults
    from phase_l.l23_full_population_funnel import load_store

    SID = "9797265001"
    OID = "b5d99d0c52d52b22"
    TITLE = "28010 - Copy Paper"
    detail = f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view"
    upsert_group(
        "8411151",
        name="Empire State Purchasing Group",
        status=STATUS_REGISTERED,
        relevant=True,
        portal_account="bidnet",
    )
    try:
        seed_bidnet_defaults()
        bind_membership(
            PORTAL_BIDNET,
            "8411151",
            kind="purchasing_group",
            status=STATUS_REGISTERED,
            name="Empire State Purchasing Group",
        )
    except Exception:
        pass
    store = load_store() or {}
    l23 = dict(store.get(OID) or {})
    report: dict[str, Any] = {
        "kind": "ProductionRegressionCopyPaper",
        "build": BUILD,
        "at": _utc(),
        "opportunity_id": OID,
        "solicitation_id": SID,
        "path": "BidNetAuthenticatedClient.discover_attachment_links+extract_schedules_from_package",
        "also_exercised": "recover_one_bounded",
    }
    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    try:
        auth = client.ensure_authenticated()
        report["auth"] = {"status": auth.status, "authenticated": bool(auth.authenticated)}
        if not auth.authenticated:
            report["ok"] = False
            report["blocker"] = "AUTH_FAILED"
            report["gate"] = "FAIL"
            return report

        # Primary production doc path (same client used by full-scale worker)
        try:
            import xlrd  # noqa: F401

            report["xlrd_available"] = True
        except Exception:
            report["xlrd_available"] = False
        docs = client.discover_attachment_links(detail, timeout_ms=60_000) or []
        downloaded = [d for d in docs if isinstance(d, dict) and d.get("local_path")]
        sched = extract_schedules_from_package(docs, opportunity_id=OID, body_text=TITLE)
        rows = [r for r in (sched.get("schedule_rows") or []) if isinstance(r, dict)]

        def _qty(r: dict) -> Any:
            q = r.get("QTY") or r.get("quantity") or r.get("Quantity")
            if isinstance(q, dict):
                return q.get("VALUE")
            return q

        def _desc(r: dict) -> str:
            d = r.get("DESCRIPTION") or r.get("description") or r.get("Item Description") or ""
            if isinstance(d, dict):
                d = d.get("VALUE") or ""
            return str(d or "").strip()

        fully = sum(1 for r in rows if _qty(r) not in (None, "") and _desc(r))
        xls_docs = [
            d
            for d in downloaded
            if re.search(r"\.xls$", str(d.get("local_path") or d.get("document_name") or ""), re.I)
        ]
        report["direct"] = {
            "document_references": len(docs),
            "files_downloaded": len(downloaded),
            "xls_files": len(xls_docs),
            "product_lines_extracted": len(rows),
            "fully_specified_product_lines": fully,
            "parsers_used": list(sched.get("parsers_used") or [])[:12],
            "sample_docs": [
                {
                    "name": d.get("document_name") or d.get("filename"),
                    "local_path_set": bool(d.get("local_path")),
                    "retrieval_status": d.get("retrieval_status"),
                }
                for d in docs[:6]
                if isinstance(d, dict)
            ],
        }

        # Also exercise bounded recovery used by the funnel worker
        bounded = recover_one_bounded(
            {
                "opportunity_id": OID,
                "title": TITLE,
                "authoritative_url": detail,
                "buyer": l23.get("buyer") or "Empire State",
            },
            auth_client=client,
            l23=l23,
            timeout_s=90.0,
            isolate_process=False,
        )
        report["bounded"] = {
            "primary_blocker": bounded.get("primary_blocker"),
            "document_count": len(bounded.get("documents") or []),
            "line_count": len(bounded.get("line_items") or []),
            "access_note": bounded.get("access_note"),
            "timed_out": bounded.get("timed_out"),
            "discover_url": bounded.get("discover_url"),
            "document_links_discovered": bounded.get("document_links_discovered"),
            "discover_error": bounded.get("discover_error"),
            "package_state": bounded.get("package_state"),
            "detail_status": bounded.get("detail_status"),
        }

        # Gate: live download + full schedule, OR bounded path matching that bar,
        # OR cached verified XLS when live docs exist but schedule under-extracted.
        bounded_fully = 0
        for li in bounded.get("line_items") or []:
            if not isinstance(li, dict):
                continue
            if _qty(li) not in (None, "") and _desc(li):
                bounded_fully += 1
        live_ok = len(downloaded) >= 4 and fully >= 40
        bounded_ok = (
            len(bounded.get("documents") or []) >= 4
            and (len(bounded.get("line_items") or []) >= 40 or bounded_fully >= 40)
        )
        partial_ok = (len(downloaded) >= 4 or len(bounded.get("documents") or []) >= 4) and max(
            fully, bounded_fully
        ) >= 35
        # Cached proof: if live downloaded the Bid Response XLS but lines are low,
        # re-extract from on-disk paths (distinguishes install/parser issues).
        cached_proof = None
        if not live_ok and len(downloaded) >= 4:
            try:
                from product_identity.spreadsheet import extract_spreadsheet

                for d in xls_docs:
                    lp = Path(str(d.get("local_path")))
                    if not lp.exists():
                        continue
                    xrows = extract_spreadsheet(lp) or []
                    xf = sum(1 for r in xrows if _qty(r) not in (None, "") and _desc(r))
                    cached_proof = {
                        "path": str(lp)[-120:],
                        "rows": len(xrows),
                        "fully_specified": xf,
                        "mode": "live_file_reparse",
                    }
                    if xf >= 40:
                        live_ok = True
                        fully = max(fully, xf)
                        report["direct"]["fully_specified_product_lines"] = fully
                        report["direct"]["product_lines_extracted"] = max(
                            int(report["direct"].get("product_lines_extracted") or 0), len(xrows)
                        )
                        break
            except Exception as exc:
                cached_proof = {"error": type(exc).__name__}
        if cached_proof:
            report["cached_proof"] = cached_proof
        report["ok"] = bool(live_ok or bounded_ok or partial_ok)
        report["gate"] = "PASS" if report["ok"] else "FAIL"
        report["evidence_mode"] = (
            "live"
            if live_ok
            else ("bounded" if bounded_ok else ("partial" if partial_ok else "insufficient"))
        )
        _save(f"{OUT}/m3_fspf_v2_regression_copy_paper.json", report)
        return report
    except Exception as exc:
        report["ok"] = False
        report["gate"] = "FAIL"
        report["error"] = type(exc).__name__
        report["message"] = str(exc)[:200]
        _save(f"{OUT}/m3_fspf_v2_regression_copy_paper.json", report)
        return report
    finally:
        try:
            client.close()
        except Exception:
            pass


def _pg_id_from_url(url: Any) -> str | None:
    try:
        from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url

        return extract_pg_id_from_url(str(url or ""))
    except Exception:
        return None


def _real_line_count(row: dict[str, Any]) -> int:
    """Count product lines that are not title-fallback placeholders."""
    lines = row.get("line_items") or []
    n = 0
    for li in lines:
        if not isinstance(li, dict):
            continue
        if li.get("source") == "title_fallback" or li.get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK":
            continue
        n += 1
    return n


def _map_result(row: dict[str, Any]) -> str:
    # One-pass funnel outcomes take precedence
    fout = str(row.get("funnel_outcome") or "")
    if fout == "SOFTWARE_ERROR" and row.get("defer"):
        return "DEFERRED_ERROR"
    if fout == "EXTERNAL_DEPENDENCY" and row.get("defer"):
        return "DEFERRED_EXTERNAL"
    if fout == "BUSINESS_REJECT":
        return "INELIGIBLE_EXIT"
    if fout == "EXPIRED":
        return "EXPIRED_EXIT"
    blocker = str(row.get("terminal_blocker") or row.get("package_blocker") or "")
    if row.get("timed_out") or row.get("bounded_timed_out") or "TIMEOUT" in blocker:
        return "TIMEOUT"
    if "CREDENTIAL" in blocker or "AUTH" in blocker or blocker == "BIDNET_AUTH_REQUIRED":
        return "AUTH_FAILED"
    pg_reg = row.get("purchasing_group_registration") or {}
    pg_st = str(pg_reg.get("status") or "").upper()
    if blocker == "AWAITING_PURCHASING_GROUP_REGISTRATION" or pg_st in {
        "QUEUED",
        "REGISTERING",
    }:
        return "NO_PACKAGE"  # retryable — group queue owns registration
    if (
        row.get("owner_action_required")
        or blocker in {"AWAITING_OWNER_ACTION", "AWAITING_OWNER_APPROVED_OUTREACH"}
        or pg_st in {"OWNER_APPROVAL_REQUIRED", "PREPARED"}
    ):
        return "OWNER_ACTION_REQUIRED"
    if blocker in {
        "SOURCE_REQUIRES_PAID_ENTITLEMENT",
        "SOURCE_REQUIRES_PAID_ENTITLEMENT_OR_LOGIN",
        "PURCHASING_GROUP_REGISTRATION_REQUIRED",
        "AWAITING_OWNER_APPROVED_OUTREACH",
        "AWAITING_OWNER_ACTION",
    } or blocker.startswith("PAID_ENTITLEMENT"):
        if "OWNER" in blocker or blocker in {
            "AWAITING_OWNER_APPROVED_OUTREACH",
            "AWAITING_OWNER_ACTION",
        }:
            return "OWNER_ACTION_REQUIRED"
        return "NO_PACKAGE" if not row.get("complete_spec") else "PARTIAL"
    docs_dl = int(row.get("documents_downloaded") or 0)
    real_lines = _real_line_count(row)
    if row.get("complete_spec") and docs_dl > 0 and real_lines > 0:
        return "SUCCESS"
    # Never treat title-fallback-only rows as PARTIAL progress
    if row.get("complete_spec") or (docs_dl > 0 and real_lines > 0):
        return "PARTIAL"
    if row.get("package_status") in {"DOCUMENTS_RECOVERED", "FREE_PACKAGE_FOUND", "AUTHENTICATED_PACKAGE_DOCS"} and (
        docs_dl > 0 or real_lines > 0
    ):
        return "PARTIAL"
    if "EXPIRED" in blocker or row.get("deadline_status") == "CLOSED_CONFIRMED":
        return "EXPIRED_EXIT"
    if row.get("fetch_error") or row.get("process_bidnet_error"):
        return "FETCH_FAILED"
    if blocker == "INCOMPLETE_LINE_ITEMS" and docs_dl == 0 and real_lines == 0:
        return "NO_PACKAGE"
    return "NO_PACKAGE"


def _slim_checkpoint(
    row: dict[str, Any],
    *,
    result: str,
    index: int,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from buyer_intelligence.commercial_funnel_execution_v1 import _line_desc, _line_qty

    rfq = row.get("rfq") if isinstance(row.get("rfq"), dict) else {}
    econ = row.get("economics") if isinstance(row.get("economics"), dict) else {}
    docs = row.get("recovered_documents") or []
    lines = row.get("line_items") or []
    fully_specified = 0
    for li in lines:
        if not isinstance(li, dict):
            continue
        if li.get("source") == "title_fallback" or li.get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK":
            continue
        qty = _line_qty(li)
        desc = _line_desc(li)
        if qty not in (None, "") and desc:
            fully_specified += 1
    retry_count = int((prior or {}).get("RETRY_COUNT") or 0)
    if (prior or {}).get("RESULT") == "PENDING_RETRY" or (prior or {}).get("RETRYABLE"):
        retry_count += 1
    blocker = row.get("terminal_blocker") or row.get("package_blocker")
    return {
        "OPPORTUNITY_ID": row.get("opportunity_id"),
        "RESULT": result,
        "INDEX": index,
        "title": (row.get("title") or "")[:160],
        "buyer": row.get("buyer"),
        "deadline": row.get("deadline"),
        "package_status": row.get("package_status"),
        "terminal_blocker": blocker,
        "RETRYABLE": bool(
            result not in _PRESERVE_RESULTS
            and (
                str(blocker or "") in _RETRYABLE_BLOCKERS
                or str(blocker or "").startswith("ABSTRACT_")
                or str(blocker or "").startswith("DETAIL_")
                or str(blocker or "").startswith("WAF_")
                or str(blocker or "").startswith("MISSING_")
                or result in {"TIMEOUT", "FETCH_FAILED", "NO_PACKAGE", "PARSE_FAILED"}
            )
        ),
        "RETRY_COUNT": retry_count,
        "PREVIOUS_RESULT": (prior or {}).get("PREVIOUS_RESULT") or (prior or {}).get("RESULT"),
        "PREVIOUS_BLOCKER": (prior or {}).get("PREVIOUS_BLOCKER")
        or (prior or {}).get("terminal_blocker"),
        "document_count": int(row.get("document_count") or len(docs) or 0),
        "documents_downloaded": int(
            row.get("documents_downloaded")
            or sum(
                1
                for d in docs
                if isinstance(d, dict) and (d.get("local_path_set") or d.get("local_path"))
            )
        ),
        "line_count": int(row.get("line_count") or len(lines) or 0),
        "fully_specified_product_lines": fully_specified,
        "complete_spec": bool(row.get("complete_spec")),
        "distributors": [
            d.get("name") for d in (row.get("distributors") or []) if isinstance(d, dict)
        ][:8],
        "supplier_status": row.get("supplier_status"),
        "public_unit_price": row.get("public_unit_price"),
        "historical_unit_price": row.get("historical_unit_price"),
        "public_price_evidence_count": int(
            (row.get("price_research") or {}).get("price_evidence_count")
            or len(row.get("public_price_evidence") or [])
            or 0
        ),
        "award_unit": row.get("award_unit")
        or (row.get("awardable_scope") or {}).get("award_unit"),
        "complete_awardable_lot": bool(row.get("complete_awardable_lot")),
        "complete_awardable_package": bool(row.get("complete_awardable_package")),
        "rfq_status": rfq.get("status"),
        "rfq_ready_to_send": False,
        "econ_state": row.get("econ_state") or econ.get("econ_state"),
        "financeable": False,
        "bid_ready": False,
        "elapsed_s": row.get("elapsed_s"),
        "fetch_via": row.get("fetch_via"),
        "is_bidnet_url": row.get("is_bidnet_url"),
        "bidnet_auth_status": row.get("bidnet_auth_status"),
        "bounded_blocker": row.get("bounded_blocker"),
        "pg_registered_awaiting_docs": bool(row.get("pg_registered_awaiting_docs")),
        "access_note": (row.get("access_note") or "")[:120] or None,
        "early_search_status": ((row.get("early_search_to_detail") or {}).get("status")),
        "early_search_query": ((row.get("early_search_to_detail") or {}).get("query")),
        "title_harvest_ok": bool((row.get("title_harvest") or {}).get("ok")),
        "title_harvest_title": ((row.get("title_harvest") or {}).get("title")),
        "statewide_fallback": row.get("statewide_fallback"),
        "search_to_detail_status": (
            ((row.get("search_to_detail_retry") or {}).get("status"))
            or ((row.get("search_to_detail_docs") or {}).get("status"))
            or ((row.get("search_to_detail") or {}).get("status"))
        ),
        "bidnet_solicitation_id": row.get("bidnet_solicitation_id")
        or ((row.get("early_search_to_detail") or {}).get("solicitation_id")),
        "process_bidnet_error": (row.get("process_bidnet_error") or "")[:160] or None,
        "package_blocker": (row.get("package_blocker") or "")[:160] or None,
        "authoritative_url": (row.get("authoritative_url") or "")[:240] or None,
        "purchasing_group_id": row.get("purchasing_group_id")
        or (row.get("purchasing_group_gate") or {}).get("purchasing_group_id")
        or (row.get("purchasing_group_registration") or {}).get("purchasing_group_id")
        or _pg_id_from_url(row.get("authoritative_url")),
        "purchasing_group_registration_status": (
            (row.get("purchasing_group_registration") or {}).get("status")
        ),
        "portal_account": row.get("portal_account")
        or (row.get("purchasing_group_registration") or {}).get("portal_account"),
        "funnel_outcome": row.get("funnel_outcome"),
        "stage_reached": row.get("stage_reached"),
        "last_completed_stage": row.get("last_completed_stage")
        or (prior or {}).get("last_completed_stage"),
        "next_stage": row.get("next_stage") or (prior or {}).get("next_stage"),
        "commercial_resume": bool(
            row.get("commercial_resume") or (prior or {}).get("commercial_resume")
        ),
        "source_outcome": row.get("source_outcome"),
        "economics_complete": bool(row.get("economics_complete")),
        "stage_timings": row.get("stage_timings")
        if isinstance(row.get("stage_timings"), dict)
        else None,
        "stage_status": row.get("stage_status")
        if isinstance(row.get("stage_status"), dict)
        else None,
        # Persist product evidence so commercial resume skips BidNet re-download.
        "line_items": (lines[:40] if lines else (prior or {}).get("line_items") or [])[:40],
        "recovered_documents": (
            docs[:20] if docs else (prior or {}).get("recovered_documents") or []
        )[:20],
        "one_pass": bool(row.get("one_pass")),
        "software_defect_key": row.get("software_defect_key"),
        "shared_access_key": row.get("shared_access_key"),
        "defer": bool(row.get("defer")),
        "CHECKPOINT_AT": _utc(),
    }


def _aggregate_scorecard(job: dict[str, Any], items: list[dict[str, Any]], queue: dict[str, Any]) -> dict[str, Any]:
    blockers: Counter[str] = Counter()
    docs = 0
    packages = 0
    lines = 0
    fully = 0
    distributors = 0
    public_prices = 0
    hist_prices = 0
    rfqs = 0
    positive_margins = 0
    awardable_lots = 0
    awardable_packages = 0
    awaiting_pg = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        b = str(it.get("terminal_blocker") or it.get("RESULT") or "UNKNOWN")
        blockers[b] += 1
        docs += int(it.get("documents_downloaded") or 0)
        if int(it.get("document_count") or 0) > 0 or it.get("package_status") in {
            "DOCUMENTS_RECOVERED",
            "FREE_PACKAGE_FOUND",
            "AUTHENTICATED_PACKAGE_DOCS",
        }:
            packages += 1
        lines += int(it.get("line_count") or 0)
        fully += int(it.get("fully_specified_product_lines") or 0)
        distributors += len(it.get("distributors") or [])
        if it.get("public_unit_price") not in (None, "") or int(
            it.get("public_price_evidence_count") or 0
        ) > 0:
            public_prices += 1
        if it.get("historical_unit_price") not in (None, ""):
            hist_prices += 1
        if it.get("rfq_status") == "AWAITING_OWNER_APPROVED_OUTREACH":
            rfqs += 1
        if it.get("complete_awardable_lot"):
            awardable_lots += 1
        if it.get("complete_awardable_package"):
            awardable_packages += 1
        if str(it.get("terminal_blocker") or "") in {
            "AWAITING_PURCHASING_GROUP_REGISTRATION",
            "PURCHASING_GROUP_REGISTRATION_REQUIRED",
        }:
            awaiting_pg += 1
        if it.get("econ_state") in {
            "PROFITABLE_BUT_QUOTE_MISSING",
            "PROFITABLE_BUT_FINANCING_UNVERIFIED",
            "VERIFIED_PROFITABLE_AND_FINANCEABLE",
            "PRELIMINARY_MARGIN_PACKAGE_SCOPE",
        }:
            positive_margins += 1

    pg_registered = 0
    pg_pending_queue = 0
    pg_unlocked = 0
    try:
        from bidnet_auth.purchasing_group_tracker import ui_summary as pg_ui

        pgui = pg_ui() or {}
        groups = list(pgui.get("groups") or [])
        pg_registered = sum(
            1 for g in groups if str((g or {}).get("status") or "").upper() == "REGISTERED"
        )
        metrics_pg = pgui.get("metrics") if isinstance(pgui.get("metrics"), dict) else {}
        if not pg_registered and metrics_pg.get("registered"):
            pg_registered = int(metrics_pg.get("registered") or 0)
    except Exception:
        pass
    try:
        from bidnet_auth.purchasing_group_queue import load_queue as load_pg_queue

        pq = load_pg_queue() or {}
        pg_pending_queue = len(pq.get("pending") or [])
        pg_unlocked = int((pq.get("metrics") or {}).get("opportunities_released") or 0)
    except Exception:
        pass
    pg_access_mgr: dict[str, Any] = {}
    try:
        from bidnet_auth.purchasing_group_access_manager_v1 import manager_status

        pg_access_mgr = manager_status() or {}
    except Exception:
        pg_access_mgr = {}

    completed = int(job.get("COMPLETED") or 0)
    # Queue file is authoritative after L23 appends; job.TOTAL may lag until saved.
    total = max(int(job.get("TOTAL") or 0), int(queue.get("eligible_total") or 0))
    if int(queue.get("eligible_total") or 0) > int(job.get("TOTAL") or 0):
        total = int(queue.get("eligible_total") or total)
    # Prefer live progress heartbeat/worker — job.json may lag when volume renames fail.
    prog_live = _load(PROGRESS_FILE) or {}
    live_hb = str(prog_live.get("heartbeat_at") or "")
    job_hb = str(job.get("HEARTBEAT_AT") or "")
    prefer_prog = bool(live_hb and (not job_hb or live_hb > job_hb))
    ready_n = 0
    ready_done = 0
    try:
        from buyer_intelligence.fspf_ready_lane_v1 import load_ready_queue, ready_count

        ready_n = int(ready_count())
        ready_done = int((load_ready_queue().get("metrics") or {}).get("completed") or 0)
    except Exception:
        ready_n = int(prog_live.get("ready_queue_size") or 0)
        ready_done = 0
    scorecard = {
        "kind": "FullScaleProductionFunnelScorecard",
        "build": BUILD,
        "at": _utc(),
        "AUTO_SEND": AUTO_SEND,
        "job_id": job.get("JOB_ID"),
        "worker_id": (
            (prog_live.get("worker_id") if prefer_prog else None)
            or job.get("WORKER_ID")
            or prog_live.get("worker_id")
        ),
        "status": (
            (prog_live.get("status") if prefer_prog else None)
            or job.get("STATUS")
            or "UNKNOWN"
        ),
        "heartbeat_at": live_hb if prefer_prog else (job_hb or live_hb or None),
        "last_checkpoint_at": job.get("LAST_CHECKPOINT_AT") or live_hb or None,
        "current_opportunity_id": (
            prog_live.get("current_opportunity_id")
            or job.get("CURRENT_OPPORTUNITY_ID")
        ),
        "metrics": {
            "live_eligible_product_opportunities": total,
            "opportunities_processed": completed,
            "purchasing_groups_registered": pg_registered,
            "purchasing_groups_pending_registration": pg_pending_queue,
            "opportunities_unlocked_via_pg": pg_unlocked,
            "opportunities_awaiting_pg": awaiting_pg,
            "pg_access_manager_running": bool(pg_access_mgr.get("running")),
            "pg_access_manager_groups_completed": int(
                pg_access_mgr.get("groups_completed") or 0
            ),
            "pg_access_manager_heartbeat_at": pg_access_mgr.get("heartbeat_at"),
            "pg_unlock_recovery_eligible": int(
                (_load_deferred_queue() or {}).get("pg_unlock_eligible_count")
                or sum(
                    1
                    for x in ((_load_deferred_queue() or {}).get("items") or [])
                    if isinstance(x, dict) and x.get("recovery_eligible")
                )
            ),
            "ready_priority_opportunities": ready_n,
            "ready_completed": ready_done,
            "ready_schedule": (job.get("META") or {}).get("ready_schedule"),
            "pass_mode": (job.get("META") or {}).get("pass_mode") or "FIRST_PASS",
            "one_pass_orchestrator": True,
            "deferred_queue_size": len((_load_deferred_queue().get("items") or [])),
            "deferred_errors": sum(
                1 for it in items if str(it.get("RESULT") or "") == "DEFERRED_ERROR"
            ),
            "deferred_external": sum(
                1 for it in items if str(it.get("RESULT") or "") == "DEFERRED_EXTERNAL"
            ),
            "opportunities_with_stage": sum(1 for it in items if it.get("stage_reached")),
            "complete_solicitation_packages": packages,
            "documents_retrieved": docs,
            "fully_specified_product_lines": fully,
            "complete_bidding_lots": sum(1 for it in items if it.get("complete_spec")),
            "complete_awardable_lots": awardable_lots,
            "complete_awardable_packages": awardable_packages,
            "matched_distributors": distributors,
            "verified_public_acquisition_prices": public_prices,
            "verified_historical_government_prices": hist_prices,
            "complete_rfqs_awaiting_approval": rfqs,
            "real_supplier_quotes_received": 0,
            "documented_positive_margins": positive_margins,
            "financing_ready_opportunities": 0,
            "fully_verified_bid_ready_opportunities": 0,
            "success": int(job.get("SUCCESS") or 0),
            "partial": int(job.get("PARTIAL") or 0),
            "timeout": int(job.get("TIMEOUT") or 0),
            "failed": int(job.get("FAILED") or 0),
            "owner_action_required": sum(1 for it in items if it.get("RESULT") == "OWNER_ACTION_REQUIRED"),
        },
        "blockers_top": [{"reason": k, "count": v} for k, v in blockers.most_common(20)],
        "api_budget": _budget_snapshot(),
        "continuation": {
            "slice_size": SLICE_SIZE,
            "resume_cursor": job.get("RESUME_CURSOR"),
            "queue_source": queue.get("source"),
            "automatic_slice_continuation": True,
        },
    }
    _save(SCORECARD_FILE, scorecard)
    return scorecard


def _budget_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {
        "openai_max_per_day_usd": 1.0,
        "openai_max_per_month_usd": 30.0,
        "sam_max_calls_per_day": 10,
        "this_job_openai_calls": 0,
        "this_job_sam_calls": 0,
        "policy": "free_source_first_deterministic_extraction",
        "sam_spend_allowed_now": None,
    }
    try:
        from api_budget import can_spend_sam, get_usage_snapshot

        out["sam_spend_allowed_now"] = bool(can_spend_sam(1))
        snap = get_usage_snapshot()
        if isinstance(snap, dict):
            out["usage"] = {
                k: snap.get(k)
                for k in (
                    "sam_used_today",
                    "sam_daily_limit",
                    "screen_used_today",
                    "screen_daily_limit",
                )
            }
    except Exception:
        pass
    return out


def _research_prices_from_rfq_queue(
    *,
    job_id: str,
    max_items: int = 3,
    budget_s: float = 55.0,
) -> dict[str, Any]:
    """Bounded public-price research for RFQs that already have complete specs.

    Closes the gap where packages/lines were recovered before live pricing was
    wired into process_one. Never invents prices; updates slim checkpoints.
    """
    from buyer_intelligence.commercial_funnel_execution_v1 import (
        _research_public_prices_for_lines,
    )

    t0 = time.time()
    blob = _load(RFQ_QUEUE_FILE) or {}
    items = list(blob.get("items") or blob.get("rows") or [])
    store = JobStore(FAMILY)
    researched = 0
    priced = 0
    examples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for entry in items:
        if researched >= max_items or time.time() - t0 >= budget_s:
            break
        if not isinstance(entry, dict):
            continue
        rfq = entry.get("rfq") if isinstance(entry.get("rfq"), dict) else entry
        if rfq.get("status") != "AWAITING_OWNER_APPROVED_OUTREACH":
            continue
        if rfq.get("public_unit_price") not in (None, "") or entry.get(
            "public_unit_price"
        ) not in (None, ""):
            continue
        oid = str(entry.get("opportunity_id") or rfq.get("opportunity_id") or "")
        lines = list(rfq.get("line_items") or entry.get("line_items") or [])
        if not oid or not lines:
            continue
        remain = max(8.0, budget_s - (time.time() - t0))
        researched += 1
        pr = _research_public_prices_for_lines(
            opportunity_id=oid,
            lines=lines,
            budget_s=min(35.0, remain),
            max_lines=2,
        )
        if pr.get("verified_public_unit_price") not in (None, ""):
            priced += 1
            rfq["public_unit_price"] = pr["verified_public_unit_price"]
            rfq["public_price_evidence"] = (pr.get("prices") or [])[:3]
            entry["public_unit_price"] = pr["verified_public_unit_price"]
            if isinstance(entry.get("rfq"), dict):
                entry["rfq"] = rfq
            hit = (pr.get("prices") or [{}])[0]
            examples.append(
                {
                    "opportunity_id": oid,
                    "unit_price": pr.get("verified_public_unit_price"),
                    "supplier": hit.get("supplier"),
                    "url": hit.get("url"),
                    "mpn": hit.get("mpn"),
                    "description": (hit.get("description") or "")[:120] or None,
                }
            )
            prior = store.load_item(job_id, oid) or {"OPPORTUNITY_ID": oid}
            prior["public_unit_price"] = pr["verified_public_unit_price"]
            prior["public_price_evidence_count"] = int(pr.get("price_evidence_count") or 0)
            if hit.get("supplier"):
                dists = list(prior.get("distributors") or [])
                if hit["supplier"] not in dists:
                    dists = [hit["supplier"]] + dists
                prior["distributors"] = dists[:8]
            prior["CHECKPOINT_AT"] = _utc()
            store.write_item_checkpoint(job_id, prior)
        else:
            failures.append(
                {
                    "opportunity_id": oid,
                    "failure_classes": list(
                        {
                            str(f.get("failure_class") or f.get("status") or "UNKNOWN")
                            for f in (pr.get("failures") or [])
                            if isinstance(f, dict)
                        }
                    )[:6],
                }
            )
    if researched:
        if "items" in blob or blob.get("items") is not None:
            blob["items"] = items
        else:
            blob["rows"] = items
        blob["updated_at"] = _utc()
        _save(RFQ_QUEUE_FILE, blob)
    return {
        "researched": researched,
        "priced": priced,
        "examples": examples,
        "failures": failures[:8],
        "elapsed_s": round(time.time() - t0, 2),
    }


def _append_rfq(row: dict[str, Any]) -> None:
    rfq = row.get("rfq")
    if not isinstance(rfq, dict):
        return
    if rfq.get("status") != "AWAITING_OWNER_APPROVED_OUTREACH":
        return
    blob = _load(RFQ_QUEUE_FILE) or {
        "kind": "FullScaleProductionFunnelRFQQueue",
        "build": BUILD,
        "AUTO_SEND": False,
        "items": [],
    }
    items = list(blob.get("items") or [])
    oid = str(row.get("opportunity_id") or "")
    items = [x for x in items if str(x.get("opportunity_id") or "") != oid]
    items.append(
        {
            "opportunity_id": oid,
            "title": (row.get("title") or "")[:160],
            "buyer": row.get("buyer"),
            "deadline": row.get("deadline"),
            "rfq": {
                **rfq,
                "AUTO_SEND": False,
                "ready_to_send": False,
                "line_items": (rfq.get("line_items") or [])[:40],
            },
            "queued_at": _utc(),
        }
    )
    blob["items"] = items[-500:]
    blob["count"] = len(blob["items"])
    blob["updated_at"] = _utc()
    _save(RFQ_QUEUE_FILE, blob)


def _contiguous_resume_cursor(
    store: Any,
    job_id: str,
    rows: list[dict[str, Any]],
    *,
    from_index: int,
) -> int:
    """Advance cursor through indices that are now skippable; stop at first pending."""
    c = max(0, int(from_index))
    while c < len(rows):
        oid = str((rows[c] or {}).get("opportunity_id") or "")
        prev = store.load_item(job_id, oid) if oid else None
        if not _item_should_skip(prev, first_pass=True):
            break
        c += 1
    return c


def _first_unattempted_index(
    store: Any,
    job_id: str,
    rows: list[dict[str, Any]],
    *,
    from_index: int = 0,
) -> int:
    """Index of first opportunity not yet attempted in first-pass; len(rows) if none."""
    return _contiguous_resume_cursor(store, job_id, rows, from_index=from_index)


def _backfill_deferred_from_attempted(
    store: Any,
    job_id: str,
    rows: list[dict[str, Any]],
) -> int:
    """Ensure previously attempted non-terminal failures are in the deferred queue."""
    dq = _load_deferred_queue()
    known = {str(x.get("opportunity_id")) for x in (dq.get("items") or [])}
    added = 0
    for i, item in enumerate(rows):
        oid = str(item.get("opportunity_id") or "")
        if not oid or oid in known:
            continue
        prev = store.load_item(job_id, oid)
        if not prev:
            continue
        result = str(prev.get("RESULT") or "")
        if not result or result in _PRESERVE_RESULTS:
            continue
        if result in {"SUCCESS", "PARTIAL", "SKIPPED_ALREADY_COMPLETE"}:
            continue
        # Already-attempted technical / external outcomes → second pass only
        _defer_opportunity(
            opportunity_id=oid,
            stage=prev.get("stage_reached") or prev.get("STAGE_REACHED"),
            outcome=str(prev.get("funnel_outcome") or result),
            defect_key=prev.get("software_defect_key"),
            shared_access_key=prev.get("shared_access_key"),
            blocker=prev.get("terminal_blocker") or prev.get("TERMINAL_BLOCKER"),
            index=int(prev.get("INDEX") if prev.get("INDEX") is not None else i),
        )
        known.add(oid)
        added += 1
    return added


def _collect_first_pass_pending(
    store: Any,
    job_id: str,
    rows: list[dict[str, Any]],
    *,
    cursor: int,
    slice_size: int,
    registered_pgs: set[str],
) -> tuple[list[tuple[int, dict[str, Any]]], list[tuple[int, dict[str, Any]]], int]:
    """Collect next first-pass slice. Continues past skipped regions until work is found."""
    pending: list[tuple[int, dict[str, Any]]] = []
    registered_pending: list[tuple[int, dict[str, Any]]] = []
    start = max(0, int(cursor))
    scan_end = start
    for j in range(start, len(rows)):
        scan_end = j + 1
        item = rows[j]
        oid = str(item.get("opportunity_id") or "")
        prev = store.load_item(job_id, oid) if oid else None
        if _item_should_skip(prev, first_pass=True):
            continue
        pg = _pg_id_from_url(item.get("authoritative_url"))
        if pg and pg in registered_pgs:
            registered_pending.append((j, item))
        else:
            pending.append((j, item))
        if len(registered_pending) >= slice_size:
            break
        if len(registered_pending) + len(pending) >= slice_size * 3 and registered_pending:
            break
        if len(pending) >= slice_size * 3 and not registered_pending:
            break

    out = (registered_pending[:slice_size] + pending)[:slice_size]
    out.sort(key=lambda t: t[0])
    return out, registered_pending, scan_end


def _heal_false_first_pass_exhaustion(
    store: Any,
    job: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, bool, int]:
    """If deferred mode was entered while unattempted work remains, restore FIRST_PASS."""
    meta = dict(job.get("META") or {})
    pass_mode = str(meta.get("pass_mode") or "FIRST_PASS")
    cursor = int(job.get("RESUME_CURSOR") or 0)
    job_id = str(job.get("JOB_ID") or "")
    next_i = _first_unattempted_index(store, job_id, rows, from_index=0)
    if pass_mode == "DEFERRED_RECOVERY" and next_i < len(rows):
        meta["pass_mode"] = "FIRST_PASS"
        meta["healed_false_exhaustion_at"] = _utc()
        meta["healed_from_cursor"] = cursor
        meta["healed_to_cursor"] = next_i
        job["RESUME_CURSOR"] = next_i
        job["META"] = meta
        store.save_job(job)
        return job, "FIRST_PASS", True, next_i
    if pass_mode != "DEFERRED_RECOVERY" and cursor > next_i and next_i < len(rows):
        # Cursor jumped past unattempted work (prior bug) — pull back
        job["RESUME_CURSOR"] = next_i
        meta["cursor_healed_at"] = _utc()
        meta["healed_from_cursor"] = cursor
        meta["healed_to_cursor"] = next_i
        job["META"] = meta
        store.save_job(job)
        return job, pass_mode or "FIRST_PASS", True, next_i
    return job, pass_mode, pass_mode != "DEFERRED_RECOVERY", cursor


def process_slice(
    *,
    job_id: str,
    worker_id: str,
    slice_size: int = SLICE_SIZE,
    lease_generation: int | None = None,
) -> dict[str, Any]:
    """Process the next slice via one-pass orchestrator (first-pass, then deferred)."""
    from buyer_intelligence.commercial_evidence_index_v1 import build_commercial_evidence_index
    from buyer_intelligence.commercial_funnel_execution_v1 import (
        _cookies_client,
        _open_bidnet_auth_client,
    )
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import run_one_opportunity
    from phase_l.l23_full_population_funnel import load_store

    store = JobStore(FAMILY)
    job = store.load_job(job_id)
    if not job:
        raise KeyError(job_id)
    if lease_generation is None:
        lease_generation = int(job.get("LEASE_GENERATION") or 0)
    if not _is_active_lease_worker(
        job_id, worker_id, lease_generation=lease_generation
    ):
        return {
            "slice_complete": False,
            "processed_this_slice": 0,
            "queue_exhausted": False,
            "job_id": job_id,
            "lease_superseded": True,
        }

    # Slice-wide heartbeat pulse — covers queue load / PG rebuild / heal paths
    # that previously ran >2 minutes with zero heartbeats and got STALE-killed.
    slice_hb_stop = threading.Event()
    slice_hb_thread = threading.Thread(
        target=_heartbeat_pulse,
        kwargs={
            "job_id": job_id,
            "worker_id": worker_id,
            "lease_generation": lease_generation,
            "current_index": job.get("CURRENT_INDEX"),
            "current_opportunity_id": job.get("CURRENT_OPPORTUNITY_ID"),
            "stop_event": slice_hb_stop,
        },
        name=f"fspf-slice-hb-{worker_id[-6:]}",
        daemon=True,
    )
    slice_hb_thread.start()

    def _stop_slice_hb() -> None:
        slice_hb_stop.set()

    try:
        store.heartbeat(
            job_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            current_index=job.get("CURRENT_INDEX"),
            current_opportunity_id=job.get("CURRENT_OPPORTUNITY_ID"),
        )
        _progress(
            status="RUNNING",
            phase="SLICE_SETUP",
            job_id=job_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
        )
    except JobLeaseError:
        _stop_slice_hb()
        return {
            "slice_complete": False,
            "processed_this_slice": 0,
            "queue_exhausted": False,
            "job_id": job_id,
            "lease_superseded": True,
        }

    queue = _load(QUEUE_FILE) or build_eligible_queue()
    rows = queue.get("rows") or []
    try:
        store.heartbeat(
            job_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
        )
    except JobLeaseError:
        _stop_slice_hb()
        return {
            "slice_complete": False,
            "processed_this_slice": 0,
            "queue_exhausted": False,
            "job_id": job_id,
            "lease_superseded": True,
        }
    job, pass_mode, first_pass, cursor = _heal_false_first_pass_exhaustion(
        store, job, rows
    )
    meta = dict(job.get("META") or {})
    # One-shot: if this job is a ground-zero new_run, drop deferred items that
    # belong to an archived prior job (prevents second-pass pollution).
    if meta.get("new_run") and not meta.get("deferred_cleared_for_new_run"):
        _save(
            DEFERRED_QUEUE_FILE,
            {
                "kind": "FullScaleDeferredQueue",
                "build": BUILD,
                "items": [],
                "by_defect": {},
                "updated_at": _utc(),
                "cleared_for_new_run": True,
                "job_id": job_id,
            },
        )
        meta["deferred_cleared_for_new_run"] = True
        meta["pass_mode"] = "FIRST_PASS"
        job["META"] = meta
        store.save_job(job)
        pass_mode = "FIRST_PASS"
        first_pass = True

    # Rebuild PG registration queue from checkpoints awaiting group access so
    # blocked solicitations are not stranded while the in-memory queue is empty.
    pg_rebuild: dict[str, Any] | None = None
    try:
        from bidnet_auth.purchasing_group_queue import rebuild_pending_from_job

        pg_rebuild = rebuild_pending_from_job(job_id)
        _progress(
            status="RUNNING",
            phase="PG_QUEUE_REBUILD",
            job_id=job_id,
            worker_id=worker_id,
            pg_rebuild=pg_rebuild,
            pass_mode=pass_mode,
        )
    except Exception as exc:
        pg_rebuild = {"ok": False, "error": type(exc).__name__}

    # Prefer opportunities whose purchasing group is already REGISTERED so
    # document unlock / line extraction runs before more group-form attempts.
    slice_start_cursor = cursor
    registered_pgs: set[str] = set()
    try:
        from bidnet_auth.purchasing_group_tracker import list_registered_group_ids

        registered_pgs = set(list_registered_group_ids() or [])
    except Exception:
        registered_pgs = set()

    pending: list[tuple[int, dict[str, Any]]] = []
    registered_pending: list[tuple[int, dict[str, Any]]] = []
    ready_schedule: dict[str, Any] = {"first_pass": 0, "ready": 0, "ratio": "n/a"}
    ready_promote_report: dict[str, Any] | None = None

    # Keep READY lane stocked from unlock-eligible deferred (lightweight, idempotent).
    try:
        from buyer_intelligence.fspf_ready_lane_v1 import (
            interleave_fair,
            pick_ready_batch,
            promote_unlock_eligible_to_ready,
            ready_count,
        )

        ready_promote_report = promote_unlock_eligible_to_ready(job_id=job_id)
    except Exception as exc:
        ready_promote_report = {"ok": False, "error": type(exc).__name__}
        interleave_fair = None  # type: ignore
        pick_ready_batch = None  # type: ignore
        ready_count = lambda: 0  # type: ignore

    if first_pass:
        pending, registered_pending, _scan_end = _collect_first_pass_pending(
            store,
            job_id,
            rows,
            cursor=cursor,
            slice_size=slice_size,
            registered_pgs=registered_pgs,
        )
        # Fair interleave: ~75% first-pass / ~25% READY unlock recovery.
        ready_batch: list[tuple[int, dict[str, Any]]] = []
        if pick_ready_batch is not None:
            try:
                ready_batch = pick_ready_batch(
                    job_id=job_id,
                    store=store,
                    rows=rows,
                    limit=max(1, slice_size),
                )
            except Exception:
                ready_batch = []
        if (pending or ready_batch) and interleave_fair is not None:
            pending, ready_schedule = interleave_fair(
                pending, ready_batch, slice_size=slice_size
            )
            if ready_schedule.get("ready"):
                pass_mode = "FIRST_PASS_WITH_READY"
                meta["pass_mode"] = pass_mode
                meta["ready_schedule"] = ready_schedule
                job["META"] = meta
                store.save_job(job)
        if not pending:
            # READY-only work still available → do not wait for first-pass exhaustion
            if ready_batch and interleave_fair is not None:
                pending, ready_schedule = interleave_fair(
                    [], ready_batch, slice_size=slice_size
                )
                pass_mode = "READY_PRIORITY"
                meta["pass_mode"] = pass_mode
                meta["ready_schedule"] = ready_schedule
                job["META"] = meta
                store.save_job(job)
            else:
                # Only after the full queue has no unattempted work → deferred recovery
                next_i = _first_unattempted_index(store, job_id, rows, from_index=0)
                if next_i < len(rows):
                    job["RESUME_CURSOR"] = next_i
                    meta["pass_mode"] = "FIRST_PASS"
                    job["META"] = meta
                    store.save_job(job)
                    pending, registered_pending, _ = _collect_first_pass_pending(
                        store,
                        job_id,
                        rows,
                        cursor=next_i,
                        slice_size=slice_size,
                        registered_pgs=registered_pgs,
                    )
                    cursor = next_i
                    slice_start_cursor = next_i
                    if pick_ready_batch is not None and interleave_fair is not None:
                        try:
                            rb2 = pick_ready_batch(
                                job_id=job_id,
                                store=store,
                                rows=rows,
                                limit=max(1, slice_size),
                            )
                            pending, ready_schedule = interleave_fair(
                                pending, rb2, slice_size=slice_size
                            )
                        except Exception:
                            pass
                else:
                    backfilled = _backfill_deferred_from_attempted(store, job_id, rows)
                    meta["pass_mode"] = "DEFERRED_RECOVERY"
                    meta["first_pass_exhausted_at"] = _utc()
                    meta["deferred_backfill_count"] = backfilled
                    job["META"] = meta
                    job["RESUME_CURSOR"] = len(rows)
                    store.save_job(job)
                    first_pass = False
                    pass_mode = "DEFERRED_RECOVERY"

    if not first_pass:
        # Second pass: READY lane first, then remaining deferred errors.
        if not (_load_deferred_queue().get("items")):
            _backfill_deferred_from_attempted(store, job_id, rows)
        try:
            reconcile_released_pg_unlocks(job_id, repair=True)
        except Exception:
            pass
        try:
            from buyer_intelligence.fspf_ready_lane_v1 import (
                pick_ready_batch as _pick_ready,
                promote_unlock_eligible_to_ready as _promote_ready,
            )

            _promote_ready(job_id=job_id)
            pending = _pick_ready(
                job_id=job_id, store=store, rows=rows, limit=slice_size
            )
            ready_schedule = {
                "first_pass": 0,
                "ready": len(pending),
                "ratio": "0/100",
            }
        except Exception:
            pending = []
        dq = _load_deferred_queue()
        raw_deferred = list(dq.get("items") or [])
        unlock_first = [
            d for d in raw_deferred if isinstance(d, dict) and d.get("recovery_eligible")
        ]
        commercial_first = [
            d
            for d in raw_deferred
            if isinstance(d, dict)
            and d.get("commercial_resume")
            and not d.get("recovery_eligible")
        ]
        rest = [
            d
            for d in raw_deferred
            if not (
                isinstance(d, dict)
                and (d.get("recovery_eligible") or d.get("commercial_resume"))
            )
        ]
        deferred_items = unlock_first + commercial_first + rest
        ready_oids_now = {str(it.get("opportunity_id")) for _, it in pending}
        for d in deferred_items[: slice_size * 3]:
            oid = str(d.get("opportunity_id") or "")
            if not oid or oid in ready_oids_now:
                continue
            prev = store.load_item(job_id, oid)
            if prev and str(prev.get("RESULT") or "") in _PRESERVE_RESULTS:
                continue
            # Skip paid entitlements / unresolved external that still lack access
            blocker = str(
                (prev or {}).get("terminal_blocker")
                or d.get("terminal_blocker")
                or ""
            )
            if blocker.startswith("PAID_ENTITLEMENT:") and not d.get("recovery_eligible"):
                continue
            if _component_circuit_tripped(d.get("software_defect_key")):
                continue
            idx = int(d.get("queue_index") or -1)
            item = rows[idx] if 0 <= idx < len(rows) else {"opportunity_id": oid}
            if str(item.get("opportunity_id") or "") != oid:
                item = {"opportunity_id": oid}
                for j, r in enumerate(rows):
                    if str(r.get("opportunity_id")) == oid:
                        item = r
                        idx = j
                        break
            item = dict(item)
            item["_prior_checkpoint"] = prev or {}
            item["commercial_resume"] = bool(
                d.get("commercial_resume") or (prev or {}).get("commercial_resume")
            )
            item["next_stage"] = (
                d.get("next_stage")
                or (prev or {}).get("next_stage")
                or d.get("stage_reached")
            )
            item["last_completed_stage"] = d.get("last_completed_stage") or (
                prev or {}
            ).get("last_completed_stage")
            if (prev or {}).get("line_items"):
                item["line_items"] = prev.get("line_items")
            if (prev or {}).get("recovered_documents"):
                item["recovered_documents"] = prev.get("recovered_documents")
            if (prev or {}).get("complete_spec"):
                item["complete_spec"] = True
            if (prev or {}).get("documents_downloaded"):
                item["documents_downloaded"] = prev.get("documents_downloaded")
            pending.append((idx, item))
            if len(pending) >= slice_size:
                break

    if not pending:
        # Truly idle only when first-pass has no unattempted work AND deferred is empty
        next_i = _first_unattempted_index(store, job_id, rows, from_index=0)
        if next_i < len(rows):
            job["RESUME_CURSOR"] = next_i
            meta["pass_mode"] = "FIRST_PASS"
            job["META"] = meta
            store.save_job(job)
            _stop_slice_hb()
            return {
                "slice_complete": True,
                "processed_this_slice": 0,
                "queue_exhausted": False,
                "job_id": job_id,
                "pass_mode": "FIRST_PASS",
                "healed_resume_cursor": next_i,
            }
        job["RESUME_CURSOR"] = len(rows)
        meta["pass_mode"] = pass_mode
        job["META"] = meta
        store.save_job(job)
        _stop_slice_hb()
        return {
            "slice_complete": True,
            "processed_this_slice": 0,
            "queue_exhausted": True,
            "job_id": job_id,
            "pass_mode": pass_mode,
        }

    _progress(
        status="RUNNING",
        phase="SLICE",
        job_id=job_id,
        worker_id=worker_id,
        slice_pending=len(pending),
        resume_cursor=cursor,
        eligible_total=len(rows),
        pass_mode=pass_mode,
        one_pass_orchestrator=True,
        deferred_queue_size=len((_load_deferred_queue().get("items") or [])),
        ready_queue_size=int(ready_count() if callable(ready_count) else 0),
        ready_schedule=ready_schedule,
        ready_promote=ready_promote_report,
    )

    evidence_index = build_commercial_evidence_index()
    l23 = load_store()
    http = _cookies_client()

    # Hydrate shared-access registry from existing checkpoints (no network).
    try:
        from buyer_intelligence.shared_access_registry_v1 import (
            hydrate_from_job_items,
            resolve_fast_access,
        )

        hydrate_report = hydrate_from_job_items(store.list_items(job_id))
    except Exception as exc:
        hydrate_report = {"error": type(exc).__name__}
        resolve_fast_access = None  # type: ignore

    # Partition: known access barriers defer without opening Playwright.
    fast_pending: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    browser_pending: list[tuple[int, dict[str, Any]]] = []
    for idx, item in pending:
        oid0 = str(item.get("opportunity_id") or "")
        try:
            from buyer_intelligence.opportunity_funnel_orchestrator_v1 import fast_qualify

            q0 = fast_qualify(item, l23_rec=l23.get(oid0) or {})
        except Exception:
            q0 = {"ok": True}
        if not q0.get("ok"):
            browser_pending.append((idx, item))  # still record via orchestrator path
            continue
        access0 = {"decision": "UNKNOWN", "defer": False}
        if resolve_fast_access is not None:
            try:
                access0 = resolve_fast_access(item, l23_rec=l23.get(oid0) or {})
            except Exception as exc:
                access0 = {"decision": "UNKNOWN", "defer": False, "error": type(exc).__name__}
        if access0.get("defer") and access0.get("access_fast_path"):
            fast_pending.append((idx, item, access0))
        else:
            browser_pending.append((idx, item))

    _progress(
        status="RUNNING",
        phase="SHARED_ACCESS_FAST_PATH",
        job_id=job_id,
        worker_id=worker_id,
        fast_path_count=len(fast_pending),
        browser_path_count=len(browser_pending),
        hydrate_report=hydrate_report,
        pass_mode=pass_mode,
    )

    processed = 0
    last_oid = None
    zero_doc_streak = 0
    registered_empty_streak = 0
    timing_accum = {
        "fast_path_count": 0,
        "fast_path_total_s": 0.0,
        "browser_path_count": 0,
        "browser_path_total_s": 0.0,
    }

    def _commit_row(idx: int, item: dict[str, Any], row: dict[str, Any]) -> str:
        nonlocal processed, last_oid, zero_doc_streak, registered_empty_streak, job
        oid_local = str(item["opportunity_id"])
        last_oid = oid_local
        result = _map_result(row)
        prior_cp = store.load_item(job_id, oid_local)
        slim = _slim_checkpoint(row, result=result, index=idx, prior=prior_cp)
        if item.get("_ready_lane"):
            slim["READY_LANE"] = True
            slim["ready_processed_at"] = _utc()
        store.write_item_checkpoint(job_id, slim)
        _append_rfq(row)
        is_ready = bool(item.get("_ready_lane"))
        if is_ready:
            try:
                from buyer_intelligence.fspf_ready_lane_v1 import (
                    demote_ready_to_deferred,
                    remove_from_ready,
                )

                if result in _PRESERVE_RESULTS | {"SUCCESS", "PARTIAL"}:
                    remove_from_ready(oid_local, reason="completed")
                elif row.get("defer") or result in {
                    "DEFERRED_ERROR",
                    "DEFERRED_EXTERNAL",
                    "TIMEOUT",
                }:
                    # Genuine error → back of the line (priority 3), leave READY.
                    _tb = str(row.get("terminal_blocker") or "")
                    demote_ready_to_deferred(
                        opportunity_id=oid_local,
                        reason=_tb or result or "READY_ERROR",
                        stage=row.get("stage_reached"),
                        blocker=row.get("terminal_blocker"),
                        defect_key=row.get("software_defect_key"),
                        # Paid entitlement after PG unlock is external — do not
                        # keep READY-eligible on a stale pg_unlocked flag.
                        keep_recovery_eligible=bool(
                            not _tb.startswith("PAID_ENTITLEMENT")
                            and (
                                row.get("recovery_eligible")
                                or item.get("recovery_eligible")
                                or _tb.startswith("PURCHASING")
                                or _tb
                                in {
                                    "DETAIL_FETCH_EMPTY",
                                    "SOLICITATION_RESOLUTION_FAILED",
                                }
                            )
                        ),
                        pg_unlocked=str(
                            row.get("purchasing_group_id")
                            or item.get("purchasing_group_id")
                            or ""
                        )
                        or None,
                        next_stage=row.get("next_stage"),
                        last_completed_stage=row.get("last_completed_stage"),
                        commercial_resume=bool(row.get("commercial_resume")),
                        queue_index=idx,
                    )
                    if row.get("software_defect_key"):
                        _note_component_defect(str(row.get("software_defect_key")))
                else:
                    # Non-terminal incomplete — keep in READY for another attempt later
                    # unless it's a hard reject
                    if result in {"EXPIRED_EXIT", "INELIGIBLE_EXIT"}:
                        remove_from_ready(oid_local, reason=result.lower())
            except Exception:
                pass
        elif row.get("defer") or result in {"DEFERRED_ERROR", "DEFERRED_EXTERNAL", "TIMEOUT"}:
            _defer_opportunity(
                opportunity_id=oid_local,
                stage=row.get("stage_reached"),
                outcome=str(row.get("funnel_outcome") or result),
                defect_key=row.get("software_defect_key"),
                shared_access_key=row.get("shared_access_key"),
                blocker=row.get("terminal_blocker"),
                index=idx,
                next_stage=row.get("next_stage"),
                last_completed_stage=row.get("last_completed_stage"),
                commercial_resume=bool(row.get("commercial_resume")),
            )
            if row.get("software_defect_key"):
                _note_component_defect(str(row.get("software_defect_key")))
        processed += 1
        docs_dl = int(slim.get("documents_downloaded") or 0)
        fetch_via = str(slim.get("fetch_via") or "")
        pg_waiting = str(slim.get("purchasing_group_registration_status") or "").upper() in {
            "QUEUED",
            "REGISTERING",
            "PREPARED",
        }
        unhealthy_fetch = fetch_via in {
            "httpx_public_or_cookie",
            "httpx_fallback_after_auth_error",
            "bidnet_auth_required",
            "bidnet_auth_fetch_failed",
            "none",
        } or result == "AUTH_FAILED"
        if docs_dl == 0 and unhealthy_fetch and not pg_waiting:
            zero_doc_streak += 1
        elif docs_dl > 0:
            zero_doc_streak = 0
        reg_empty = (
            docs_dl == 0
            and str(slim.get("terminal_blocker") or "") == "DETAIL_FETCH_EMPTY"
            and (
                str(slim.get("purchasing_group_registration_status") or "") == "REGISTERED"
                or bool(slim.get("pg_registered_awaiting_docs"))
            )
        )
        if reg_empty and slim.get("early_search_status"):
            registered_empty_streak += 1
        elif not reg_empty:
            registered_empty_streak = 0
        # Fast-path: avoid full O(n) recompute every item; batch every 10.
        do_recompute = (not row.get("access_fast_path")) or (
            timing_accum.get("fast_path_count", 0) % 10 == 0
        )
        if do_recompute:
            job = store.recompute_counts(job_id)
        else:
            job = store.load_job(job_id) or job
            job["COMPLETED"] = int(job.get("COMPLETED") or 0) + 1
        job["RESUME_CURSOR"] = _contiguous_resume_cursor(
            store, job_id, rows, from_index=slice_start_cursor
        )
        job["META"] = {
            **(job.get("META") or {}),
            "last_blocker": slim.get("terminal_blocker"),
            "last_result": result,
            "pass_mode": pass_mode,
            "one_pass_orchestrator": True,
            "stage_reached": slim.get("stage_reached"),
            "funnel_outcome": slim.get("funnel_outcome"),
            "deferred_queue_size": len((_load_deferred_queue().get("items") or [])),
            "timing_accum": timing_accum,
            "access_fast_path": bool(slim.get("access_fast_path") or row.get("access_fast_path")),
        }
        store.save_job(job)
        try:
            store.heartbeat(
                job_id,
                worker_id=worker_id,
                current_index=idx,
                current_opportunity_id=oid_local,
                lease_generation=lease_generation,
            )
        except JobLeaseError:
            return result
        return result

    # --- Fast path: no browser ---
    for idx, item, access in fast_pending:
        if _stop_flag() or not _is_active_lease_worker(
            job_id, worker_id, lease_generation=lease_generation
        ):
            break
        t0 = time.time()
        oid = str(item["opportunity_id"])
        row = {
            "opportunity_id": oid,
            "title": item.get("title"),
            "buyer": item.get("buyer"),
            "authoritative_url": item.get("authoritative_url"),
            "terminal_blocker": access.get("terminal_blocker"),
            "package_blocker": access.get("package_blocker") or access.get("terminal_blocker"),
            "funnel_outcome": access.get("funnel_outcome") or "EXTERNAL_DEPENDENCY",
            "stage_reached": "ACCESS_DOCS",
            "defer": True,
            "shared_access_key": access.get("shared_access_key"),
            "purchasing_group_id": access.get("purchasing_group_id"),
            "purchasing_group_registration_status": access.get(
                "purchasing_group_registration_status"
            ),
            "access_fast_path": True,
            "access_decision": access.get("decision"),
            "access_basis": access.get("access_basis"),
            "fetch_via": "shared_access_registry_fast_path",
            "one_pass": True,
            "elapsed_s": 0.0,
        }
        row["elapsed_s"] = round(time.time() - t0, 4)
        timing_accum["fast_path_count"] += 1
        timing_accum["fast_path_total_s"] += float(row["elapsed_s"])
        _progress(
            status="RUNNING",
            phase="SHARED_ACCESS_FAST_DEFER",
            job_id=job_id,
            worker_id=worker_id,
            current_opportunity_id=oid,
            current_index=idx,
            access_decision=access.get("decision"),
            pass_mode=pass_mode,
            timing_accum=timing_accum,
        )
        _commit_row(idx, item, row)
    if fast_pending:
        # Final accurate counts after batched fast-path writes
        job = store.recompute_counts(job_id)

    # PG registration runs on the independent access manager (separate browser).
    # Opportunity workers only open BidNet when browser_pending work exists.
    auth_status: dict[str, Any] = {}
    auth_client = None
    pg_slice_report: dict[str, Any] | None = {
        "delegated_to": "purchasing_group_access_manager_v1",
        "inline_skipped": True,
    }
    pg_ran_start = False
    try:
        from bidnet_auth.purchasing_group_access_manager_v1 import (
            ensure_access_manager_running,
            manager_status,
        )

        ensure_access_manager_running(job_id=job_id)
        pg_slice_report = {
            **pg_slice_report,
            "access_manager": manager_status(),
            "registered_unlock_priority": bool(registered_pending),
        }
    except Exception as exc:
        pg_slice_report = {**(pg_slice_report or {}), "access_manager_error": type(exc).__name__}

    def _try_unlock_micro_recovery(reason: str) -> dict[str, Any] | None:
        """Advance one unlocked opportunity when first-pass browser work cannot run."""
        if not first_pass or not _is_active_lease_worker(
            job_id, worker_id, lease_generation=lease_generation
        ):
            return None
        try:
            report = _recover_one_unlocked_opportunity(
                job_id=job_id,
                worker_id=worker_id,
                rows=rows,
                store=store,
                http=http,
                evidence_index=evidence_index,
                l23=l23,
            )
            report = {**(report or {}), "trigger": reason}
            if report.get("processed"):
                _progress(
                    status="RUNNING",
                    phase="PG_UNLOCK_MICRO_RECOVERY",
                    job_id=job_id,
                    worker_id=worker_id,
                    unlock_recovery=report,
                )
            return report
        except Exception as exc:
            return {"processed": 0, "reason": "micro_recovery_error", "error": type(exc).__name__, "trigger": reason}

    def _defer_browser_pending_back(reason: str, blocker: str) -> int:
        """Send unrun browser items to the back of the line and advance the cursor.

        Prevents infinite re-selection of the same slice while Playwright is busy
        with PG registration — first-pass must keep moving.
        """
        deferred_n = 0
        for idx, item in browser_pending:
            oid = str(item.get("opportunity_id") or "")
            if not oid:
                continue
            row = {
                "opportunity_id": oid,
                "terminal_blocker": blocker,
                # Must be EXTERNAL_DEPENDENCY so _map_result → DEFERRED_EXTERNAL
                # and first-pass skip logic advances the cursor.
                "funnel_outcome": "EXTERNAL_DEPENDENCY",
                "stage_reached": "ACCESS_DOCS",
                "defer": True,
                "software_defect_key": reason,
                "shared_access_key": item.get("shared_access_key"),
                "access_fast_path": False,
                "fetch_via": "browser_unavailable_deferred",
                "documents_downloaded": 0,
                "note": f"Deferred to back of line: {reason}",
            }
            _commit_row(idx, item, row)
            deferred_n += 1
        return deferred_n

    unlock_recovery_report: dict[str, Any] | None = None
    price_pass: dict[str, Any] | None = None
    browser_slot_held = False
    try:
        if browser_pending:
            from bidnet_auth.browser_mutex_v1 import (
                release_bidnet_browser,
                try_acquire_bidnet_browser,
            )

            # Bounded wait — do not stall first-pass for minutes behind PG registration.
            browser_slot_held = try_acquire_bidnet_browser(
                holder=f"fspf-{worker_id}", timeout_s=20.0
            )
            if not browser_slot_held:
                deferred_n = _defer_browser_pending_back(
                    "browser_slot_busy", "BROWSER_SLOT_BUSY"
                )
                unlock_recovery_report = _try_unlock_micro_recovery("browser_slot_deferred")
                _progress(
                    status="RUNNING",
                    phase="BROWSER_SLOT_DEFER_CONTINUE",
                    job_id=job_id,
                    worker_id=worker_id,
                    note="playwright_busy_browser_items_sent_to_back_of_line",
                    browser_items_deferred=deferred_n,
                    unlock_micro_recovery=unlock_recovery_report,
                )
                _stop_slice_hb()
                return {
                    "slice_complete": True,
                    "processed_this_slice": processed,
                    "queue_exhausted": False,
                    "job_id": job_id,
                    "browser_slot_deferred": True,
                    "browser_items_deferred": deferred_n,
                    "pg_group_slice": {
                        **(pg_slice_report or {}),
                        "unlock_micro_recovery": unlock_recovery_report,
                    },
                    "timing_accum": timing_accum,
                    "unlock_micro_recovery": unlock_recovery_report,
                }
            # Probe auth once on THIS thread (slot holder). Do not keep the
            # long-lived Playwright client across ThreadPool item workers —
            # Playwright sync is thread-affine and cross-thread close after
            # timeout was the root of browser_unavailable:Error.
            auth_status, probe_client = _open_bidnet_auth_client()
            if probe_client is not None:
                try:
                    probe_client.close()
                except Exception:
                    pass
                probe_client = None
            auth_client = None
            if not auth_status.get("authenticated"):
                # Release slot so PG manager / unlock recovery can use Playwright.
                if browser_slot_held:
                    try:
                        release_bidnet_browser(holder=f"fspf-{worker_id}")
                    except Exception:
                        pass
                    browser_slot_held = False
                try:
                    from bidnet_auth.auth_lifecycle_v1 import (
                        classify_auth_result_message,
                        force_cleanup_orphaned_chromium,
                    )

                    force_cleanup_orphaned_chromium(max_kill=6)
                    blocker = classify_auth_result_message(
                        status=str(auth_status.get("status") or ""),
                        message=str(auth_status.get("message") or ""),
                    )
                except Exception:
                    blocker = str(
                        auth_status.get("external_blocker")
                        or auth_status.get("status")
                        or "BIDNET_AUTH_TRANSIENT"
                    )
                deferred_n = _defer_browser_pending_back(
                    "browser_auth_transient",
                    blocker,
                )
                unlock_recovery_report = _try_unlock_micro_recovery("auth_unavailable")
                # Transient browser/auth failure must not halt the entire first pass.
                _progress(
                    status="RUNNING",
                    phase="AUTH_DEFERRED_CONTINUE",
                    last_blocker=blocker,
                    job_id=job_id,
                    worker_id=worker_id,
                    bidnet_auth_status=auth_status.get("status"),
                    auth_message=(auth_status.get("message") or "")[:200],
                    auth_failure_class=blocker,
                    timing_accum=timing_accum,
                    browser_items_deferred=deferred_n,
                    unlock_micro_recovery=unlock_recovery_report,
                    note="browser_auth_failed_items_deferred_first_pass_continues",
                )
                _stop_slice_hb()
                return {
                    "slice_complete": True,
                    "processed_this_slice": processed,
                    "queue_exhausted": False,
                    "job_id": job_id,
                    "browser_auth_deferred": True,
                    "browser_items_deferred": deferred_n,
                    "reason": blocker,
                    "auth_status": auth_status,
                    "pg_group_slice": {
                        **(pg_slice_report or {}),
                        "unlock_micro_recovery": unlock_recovery_report,
                    },
                    "timing_accum": timing_accum,
                    "unlock_micro_recovery": unlock_recovery_report,
                }
        for idx, item in browser_pending:
            if _stop_flag() or not _is_active_lease_worker(
                job_id, worker_id, lease_generation=lease_generation
            ):
                break
            oid = str(item["opportunity_id"])
            last_oid = oid
            try:
                store.heartbeat(
                    job_id,
                    worker_id=worker_id,
                    current_index=idx,
                    current_opportunity_id=oid,
                    lease_generation=lease_generation,
                )
            except JobLeaseError:
                break
            if browser_slot_held:
                try:
                    from bidnet_auth.browser_mutex_v1 import refresh_bidnet_browser_lease

                    refresh_bidnet_browser_lease(holder=f"fspf-{worker_id}", ttl_s=150.0)
                except Exception:
                    pass
            _progress(
                status="RUNNING",
                phase="ONE_PASS_FUNNEL",
                job_id=job_id,
                worker_id=worker_id,
                current_index=idx,
                current_opportunity_id=oid,
                completed=job.get("COMPLETED"),
                eligible_total=len(rows),
                pass_mode=pass_mode,
            )

            t0 = time.time()
            try:
                # ONE continuous journey with hard wall-clock. Critical: do NOT
                # use ``with ThreadPoolExecutor`` — its __exit__ waits forever for
                # hung Playwright threads after fut.result timeout (root cause of
                # multi-hour RUNNING-but-dead workers).
                #
                # Playwright sync is thread-affine: create + use + close the
                # BidNet client INSIDE the worker thread. Never close it from the
                # main thread on timeout (that produced browser_unavailable:Error).
                def _run() -> dict[str, Any]:
                    local_status, local_client = _open_bidnet_auth_client()
                    try:
                        if not local_status.get("authenticated") or local_client is None:
                            return {
                                "opportunity_id": oid,
                                "title": item.get("title"),
                                "terminal_blocker": str(
                                    local_status.get("external_blocker")
                                    or local_status.get("status")
                                    or "BROWSER_STARTUP_FAILED"
                                ),
                                "bidnet_auth_status": local_status.get("status"),
                                "auth_message": (local_status.get("message") or "")[:200],
                                "funnel_outcome": "EXTERNAL_DEPENDENCY",
                                "stage_reached": "ACCESS_DOCS",
                                "defer": True,
                                "software_defect_key": "browser_auth_transient",
                                "fetch_via": "auth_failed_in_item_thread",
                                "documents_downloaded": 0,
                                "one_pass": True,
                                "elapsed_s": round(time.time() - t0, 2),
                            }
                        return run_one_opportunity(
                            item,
                            client=http,
                            evidence_index=evidence_index,
                            auth_status=local_status,
                            l23_rec=l23.get(oid) or {},
                            auth_client=local_client,
                        )
                    finally:
                        if local_client is not None:
                            try:
                                local_client.close()
                            except Exception:
                                pass

                def _timeout_row() -> dict[str, Any]:
                    # Do NOT touch Playwright objects created in the abandoned
                    # worker thread. Kill orphan Chromium processes instead.
                    try:
                        from bidnet_auth.auth_lifecycle_v1 import (
                            force_cleanup_orphaned_chromium,
                        )

                        force_cleanup_orphaned_chromium(max_kill=6)
                    except Exception:
                        pass
                    return {
                        "opportunity_id": oid,
                        "title": item.get("title"),
                        "terminal_blocker": "PER_OPPORTUNITY_TIMEOUT",
                        "bounded_timed_out": True,
                        "funnel_outcome": "SOFTWARE_ERROR",
                        "stage_reached": "ACCESS_DOCS",
                        "defer": True,
                        "software_defect_key": "TIMEOUT",
                        "one_pass": True,
                        "elapsed_s": round(time.time() - t0, 2),
                        "browser_cleanup": "orphans_killed_no_cross_thread_close",
                    }

                hb_stop = threading.Event()
                hb_thread = threading.Thread(
                    target=_heartbeat_pulse,
                    kwargs={
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "lease_generation": lease_generation,
                        "current_index": idx,
                        "current_opportunity_id": oid,
                        "stop_event": hb_stop,
                    },
                    name=f"fspf-hb-{oid[:8]}",
                    daemon=True,
                )
                hb_thread.start()
                row: dict[str, Any] | None = None
                try:
                    row = _run_with_timeout_no_hang(
                        _run,
                        timeout_s=ITEM_TIMEOUT_S,
                        on_timeout=_timeout_row,
                    )
                finally:
                    hb_stop.set()
                    # Drop any stale outer reference; client lifetime is per-thread.
                    auth_client = None
                if not isinstance(row, dict):
                    row = _timeout_row()
                # Propagate precise browser/auth class when present
                auth_msg = str(row.get("auth_message") or "")
                if auth_msg.startswith("browser_unavailable:") or row.get(
                    "terminal_blocker"
                ) in {
                    "BROWSER_STARTUP_FAILED",
                    "BROWSER_PROCESS_FAILED",
                    "BROWSER_CONTEXT_INVALID",
                    "SESSION_RESTORE_FAILED",
                    "SESSION_EXPIRED",
                    "AUTH_REJECTED",
                }:
                    # Never reclassify as solicitation-resolution failure.
                    row["auth_failure_class"] = row.get("terminal_blocker")
                if time.time() - t0 > ITEM_TIMEOUT_S and not row.get("complete_spec"):
                    row["bounded_timed_out"] = True
                    row["terminal_blocker"] = row.get("terminal_blocker") or "PER_OPPORTUNITY_TIMEOUT"
                    row["funnel_outcome"] = row.get("funnel_outcome") or "SOFTWARE_ERROR"
                    row["defer"] = True
                    row["software_defect_key"] = row.get("software_defect_key") or "TIMEOUT"
            except Exception as exc:
                row = {
                    "opportunity_id": oid,
                    "title": item.get("title"),
                    "terminal_blocker": f"PROCESS_EXCEPTION:{type(exc).__name__}",
                    "process_error": str(exc)[:200],
                    "elapsed_s": round(time.time() - t0, 2),
                    "funnel_outcome": "SOFTWARE_ERROR",
                    "stage_reached": "ACCESS_DOCS",
                    "defer": True,
                    "software_defect_key": type(exc).__name__,
                    "one_pass": True,
                }
                log.exception("FSPF one-pass funnel failed oid=%s", oid)

            elapsed_browser = round(time.time() - t0, 3)
            row["elapsed_s"] = row.get("elapsed_s") or elapsed_browser
            timing_accum["browser_path_count"] += 1
            timing_accum["browser_path_total_s"] += float(elapsed_browser)
            result = _commit_row(idx, item, row)
            if not first_pass and result in _PRESERVE_RESULTS | {"SUCCESS", "PARTIAL"}:
                dq = _load_deferred_queue()
                dq["items"] = [
                    x
                    for x in (dq.get("items") or [])
                    if str(x.get("opportunity_id")) != oid
                ]
                dq["count"] = len(dq["items"])
                dq["updated_at"] = _utc()
                _save(DEFERRED_QUEUE_FILE, dq)
            job = store.load_job(job_id) or job
            job["META"] = {
                **(job.get("META") or {}),
                "bidnet_auth_status": auth_status.get("status"),
                "zero_doc_streak": zero_doc_streak,
                "pg_group_slice": pg_slice_report,
                "timing_accum": timing_accum,
            }
            store.save_job(job)
            if processed % 5 == 0:
                try:
                    _aggregate_scorecard(job, store.list_items(job_id), queue)
                except Exception:
                    pass

            # Skip cooldown after access fast-path (already filtered); keep for browser work
            if SOURCE_COOLDOWN_S > 0 and not row.get("access_fast_path"):
                time.sleep(SOURCE_COOLDOWN_S)

            # Hard stop on credential failure — resume later
            if result == "AUTH_FAILED" and auth_status.get("external_blocker"):
                _progress(
                    status="BLOCKED",
                    phase="AUTH_BLOCKER",
                    last_blocker=auth_status.get("external_blocker"),
                    job_id=job_id,
                )
                break
            # Circuit breaker: systemic zero-doc lane (no misleading PARTIAL flood)
            if zero_doc_streak >= 8 and processed >= 8:
                sc = _aggregate_scorecard(job, store.list_items(job_id), queue)
                metrics = (sc or {}).get("metrics") or {}
                if int(metrics.get("documents_retrieved") or 0) == 0:
                    _progress(
                        status="BLOCKED",
                        phase="CIRCUIT_BREAKER_ZERO_DOCS",
                        last_blocker="ZERO_DOCS_STREAK",
                        job_id=job_id,
                        worker_id=worker_id,
                        zero_doc_streak=zero_doc_streak,
                        last_fetch_via=fetch_via,
                    )
                    break
            # Soft circuit: hard-block registered-unlock only when the job has
            # produced no packages yet. Once docs/lines exist, reset the streak
            # and continue so PG registration + other lanes keep moving.
            if registered_empty_streak >= 10:
                try:
                    sc_soft = _aggregate_scorecard(job, store.list_items(job_id), queue)
                    pkgs = int(
                        (sc_soft.get("metrics") or {}).get("complete_solicitation_packages") or 0
                    )
                    docs_tot = int((sc_soft.get("metrics") or {}).get("documents_retrieved") or 0)
                except Exception:
                    pkgs, docs_tot = 0, 0
                if pkgs == 0 and docs_tot == 0:
                    _progress(
                        status="BLOCKED",
                        phase="CIRCUIT_BREAKER_REGISTERED_UNLOCK",
                        last_blocker="REGISTERED_DETAIL_FETCH_EMPTY_STREAK",
                        job_id=job_id,
                        worker_id=worker_id,
                        registered_empty_streak=registered_empty_streak,
                        last_fetch_via=fetch_via,
                    )
                    break
                _progress(
                    status="RUNNING",
                    phase="REGISTERED_UNLOCK_SOFT_PAUSE",
                    job_id=job_id,
                    worker_id=worker_id,
                    registered_empty_streak=registered_empty_streak,
                    note="packages_exist_continuing_slice",
                )
                registered_empty_streak = 0

        # Commercial research for already-complete RFQs (lines recovered earlier).
        # Skip expensive end-of-slice price/PG browser work when this slice only
        # performed shared-access fast deferrals (no new specs to price).
        slice_all_fast = (
            int(timing_accum.get("fast_path_count") or 0) > 0
            and int(timing_accum.get("browser_path_count") or 0) == 0
        )
        try:
            if (
                _is_active_lease_worker(
                    job_id, worker_id, lease_generation=lease_generation
                )
                and not slice_all_fast
            ):
                price_pass = _research_prices_from_rfq_queue(
                    job_id=job_id, max_items=3, budget_s=55.0
                )
                _progress(
                    status="RUNNING",
                    phase="COMMERCIAL_PRICE_PASS",
                    job_id=job_id,
                    worker_id=worker_id,
                    price_pass=price_pass,
                )
            elif slice_all_fast:
                price_pass = {"skipped": True, "reason": "slice_all_shared_access_fast_path"}
        except Exception as exc:
            price_pass = {"error": type(exc).__name__}

        # End-of-slice: PG work stays on the independent access manager.
        # Attach manager heartbeat so progress/scorecard show registration throughput.
        try:
            from bidnet_auth.purchasing_group_access_manager_v1 import manager_status

            pg_slice_report = {
                "delegated_to": "purchasing_group_access_manager_v1",
                "inline_skipped": True,
                "slice_all_fast": slice_all_fast,
                "access_manager": manager_status(),
                "price_pass": price_pass,
                "unlock_micro_recovery": unlock_recovery_report,
            }
            _progress(
                status="RUNNING",
                phase="PG_ACCESS_MANAGER_HEARTBEAT",
                job_id=job_id,
                worker_id=worker_id,
                pg_group_slice=pg_slice_report,
            )
        except Exception as exc:
            pg_slice_report = {
                "delegated_to": "purchasing_group_access_manager_v1",
                "access_manager_error": type(exc).__name__,
                "price_pass": price_pass,
                "unlock_micro_recovery": unlock_recovery_report,
            }

        # Release Playwright before unlock micro-recovery so recovery can acquire the slot.
        if browser_slot_held:
            try:
                from bidnet_auth.browser_mutex_v1 import release_bidnet_browser

                release_bidnet_browser(holder=f"fspf-{worker_id}")
            except Exception:
                pass
            browser_slot_held = False
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass
            auth_client = None

        # Recover ONE unlocked opportunity while http client is still open.
        if first_pass and not (unlock_recovery_report or {}).get("processed"):
            _progress(
                status="RUNNING",
                phase="PG_UNLOCK_MICRO_RECOVERY_START",
                job_id=job_id,
                worker_id=worker_id,
                note="post_slice_browser_released",
            )
            unlock_recovery_report = _try_unlock_micro_recovery("post_slice_browser_released")
            if (unlock_recovery_report or {}).get("processed"):
                try:
                    price_pass = _research_prices_from_rfq_queue(
                        job_id=job_id, max_items=2, budget_s=40.0
                    )
                except Exception as exc:
                    price_pass = {"error": type(exc).__name__, "after": "unlock_micro"}
    finally:
        try:
            http.close()
        except Exception:
            pass
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass
        if browser_slot_held:
            try:
                from bidnet_auth.browser_mutex_v1 import release_bidnet_browser

                release_bidnet_browser(holder=f"fspf-{worker_id}")
            except Exception:
                pass
            browser_slot_held = False

    job = store.recompute_counts(job_id)
    items = store.list_items(job_id)
    scorecard = _aggregate_scorecard(job, items, queue)
    _progress(
        status="RUNNING" if not _stop_flag() else "STOP_REQUESTED",
        phase="SLICE_DONE",
        job_id=job_id,
        worker_id=worker_id,
        processed_this_slice=processed,
        completed=job.get("COMPLETED"),
        eligible_total=job.get("TOTAL"),
        resume_cursor=job.get("RESUME_CURSOR"),
        last_opportunity_id=last_oid,
        scorecard_metrics=scorecard.get("metrics"),
        unlock_micro_recovery=unlock_recovery_report,
        price_pass=price_pass,
    )
    _stop_slice_hb()
    return {
        "slice_complete": True,
        "processed_this_slice": processed,
        "queue_exhausted": int(job.get("COMPLETED") or 0) >= int(job.get("TOTAL") or 0),
        "job_id": job_id,
        "completed": job.get("COMPLETED"),
        "total": job.get("TOTAL"),
        "scorecard": scorecard.get("metrics"),
        "unlock_micro_recovery": unlock_recovery_report,
        "price_pass": price_pass,
    }


def run_continuous(*, job_id: str, worker_id: str, slice_size: int = SLICE_SIZE) -> dict[str, Any]:
    """Run slice after slice until queue exhausted, stop, or hard blocker."""
    store = JobStore(FAMILY)
    job0 = store.load_job(job_id) or {}
    lease_generation = int(job0.get("LEASE_GENERATION") or 0)
    # Independent PG access lane — separate browser, never blocks opportunity workers.
    try:
        from bidnet_auth.purchasing_group_access_manager_v1 import ensure_access_manager_running

        ensure_access_manager_running(job_id=job_id)
    except Exception as exc:
        log.warning("pg access manager start failed: %s", type(exc).__name__)
    # Independent supervisor — stall detection outside this worker thread.
    try:
        from buyer_intelligence.fspf_supervisor_v1 import ensure_supervisor_running

        ensure_supervisor_running()
    except Exception as exc:
        log.warning("fspf supervisor start failed: %s", type(exc).__name__)
    slices = 0
    while True:
        if not _is_active_lease_worker(
            job_id, worker_id, lease_generation=lease_generation
        ):
            _progress(
                status="STOPPED",
                phase="LEASE_SUPERSEDED",
                job_id=job_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
                note="exiting_zombie_worker",
            )
            break
        if _stop_flag():
            # Only the lease holder may release / cancel the job.
            if _is_active_lease_worker(
                job_id, worker_id, lease_generation=lease_generation
            ):
                store.release_lease(job_id, worker_id, final_status="CANCELLED")
                _progress(status="CANCELLED", phase="STOPPED", job_id=job_id)
            break
        out = process_slice(
            job_id=job_id,
            worker_id=worker_id,
            slice_size=slice_size,
            lease_generation=lease_generation,
        )
        slices += 1
        if not _is_active_lease_worker(
            job_id, worker_id, lease_generation=lease_generation
        ):
            _progress(
                status="STOPPED",
                phase="LEASE_SUPERSEDED",
                job_id=job_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
            break
        try:
            store.heartbeat(
                job_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
            )
        except JobLeaseError:
            _progress(
                status="STOPPED",
                phase="LEASE_SUPERSEDED",
                job_id=job_id,
                worker_id=worker_id,
            )
            break
        if out.get("queue_exhausted"):
            store.release_lease(job_id, worker_id, final_status="DONE")
            _progress(status="DONE", phase="QUEUE_EXHAUSTED", job_id=job_id, slices=slices)
            break
        if out.get("processed_this_slice", 0) == 0:
            # No progress — avoid hot loop
            prog = job_progress()
            if prog.get("status") == "BLOCKED":
                store.release_lease(job_id, worker_id, final_status="BLOCKED")
                break
            time.sleep(2.0)
            # Advance cursor past stuck region if any
            job = store.load_job(job_id) or {}
            if int(job.get("RESUME_CURSOR") or 0) >= int(job.get("TOTAL") or 0):
                store.release_lease(job_id, worker_id, final_status="DONE")
                _progress(status="DONE", phase="CURSOR_END", job_id=job_id)
                break
        # Automatic continuation to next slice (no Cursor involvement)
        _progress(
            status="RUNNING",
            phase="CONTINUE_NEXT_SLICE",
            slices=slices,
            job_id=job_id,
            last_slice=out,
        )

    job = store.load_job(job_id) or {}
    items = store.list_items(job_id)
    queue = _load(QUEUE_FILE) or {}
    scorecard = _aggregate_scorecard(job, items, queue)
    report = {
        "kind": "FullScaleProductionFunnelReport",
        "build": BUILD,
        "at": _utc(),
        "job_id": job_id,
        "worker_id": worker_id,
        "status": job.get("STATUS"),
        "slices": slices,
        "metrics": scorecard.get("metrics"),
        "blockers_top": scorecard.get("blockers_top"),
        "AUTO_SEND": False,
        "FINAL_STATUS": job.get("STATUS"),
    }
    _save(REPORT_FILE, report)
    return report


def start_job(
    *,
    force: bool = False,
    rebuild_queue: bool = False,
    retry_technical_failures: bool = False,
    job_id: str | None = None,
    new_run: bool = False,
    archive_job_id: str | None = None,
) -> dict[str, Any]:
    """Start (or resume) the full-scale funnel as a background daemon thread.

    Returns immediately — queue build + processing run in the worker thread so
    the HTTP start endpoint never blocks on the 10k+ eligible population.

    Prefer resume of the existing job. Never rebuild the queue unless explicitly
    requested. retry_technical_failures requeues systemic failures as PENDING_RETRY.
    new_run=True archives the prior job and creates a fresh job from opportunity #1.
    """
    global _worker, _stop_requested

    with _thread_lock:
        cur = job_progress()
        if _worker and cur.get("status") == "RUNNING":
            hb = str(cur.get("heartbeat_at") or "")
            stale = True
            if hb:
                try:
                    dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    stale = (now_utc() - dt).total_seconds() > STALE_LEASE_S
                except Exception:
                    stale = True
            if not force and not stale and not new_run:
                return {
                    "ok": True,
                    "status": "ALREADY_RUNNING",
                    "build": BUILD,
                    "progress": cur,
                    "job_id": cur.get("job_id"),
                    "worker_id": cur.get("worker_id"),
                }

        store = JobStore(FAMILY)
        archive_report = None
        if new_run:
            aid = archive_job_id or (cur.get("job_id") if cur else None)
            if not aid:
                active0 = store.find_active_job(stale_seconds=86400)
                aid = (active0 or {}).get("JOB_ID")
            if aid:
                try:
                    archive_report = archive_prior_run(str(aid))
                except Exception as exc:
                    archive_report = {"ok": False, "error": type(exc).__name__, "job_id": aid}
            rebuild_queue = True
            # Fresh funnel decisions — do not carry prior deferred/circuit state
            _save(
                DEFERRED_QUEUE_FILE,
                {
                    "kind": "FullScaleDeferredQueue",
                    "build": BUILD,
                    "items": [],
                    "by_defect": {},
                    "updated_at": _utc(),
                    "cleared_for_new_run": True,
                },
            )
            _save(
                CIRCUIT_FILE,
                {"kind": "ComponentCircuits", "build": BUILD, "circuits": {}, "updated_at": _utc()},
            )

        active = None if new_run else store.find_active_job(stale_seconds=STALE_LEASE_S)
        existing_queue = None if rebuild_queue else _load(QUEUE_FILE)
        prior_total = int((existing_queue or {}).get("eligible_total") or 0)
        retry_report = None

        # Prefer explicit job_id → active → create
        if new_run:
            job = store.create_job(
                total=0,
                opportunity_ids=[],
                meta={
                    "build": BUILD,
                    "slice_size": SLICE_SIZE,
                    "AUTO_SEND": False,
                    "queue_pending": True,
                    "new_run": True,
                    "prior_archived": archive_report,
                    "resume_from_opportunity_index": 0,
                },
            )
            chosen_id = str(job["JOB_ID"])
        elif job_id and store.load_job(job_id):
            chosen_id = job_id
            job = store.load_job(job_id) or {}
        elif active and active.get("STATUS") in {
            "RUNNING",
            "CREATED",
            "INTERRUPTED",
            "STALE_RELEASED",
            "STOPPED",
            "CANCELLED",
            "BLOCKED",
            "ERROR",
            "FAILED",
        }:
            # Resume original production job even after stop/error — do not create a parallel worker job
            chosen_id = str(active["JOB_ID"])
            job = store.load_job(chosen_id) or active
        else:
            # Scan newest for the original family job before creating
            # (includes FAILED — volume write crashes must not spawn a competing job)
            chosen_id = None
            for p in sorted(store.root.glob("*/job.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
                cand = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
                if isinstance(cand, dict) and cand.get("FAMILY") == FAMILY:
                    st = str(cand.get("STATUS") or "")
                    if st in {
                        "RUNNING",
                        "CREATED",
                        "INTERRUPTED",
                        "STALE_RELEASED",
                        "STOPPED",
                        "CANCELLED",
                        "BLOCKED",
                        "ERROR",
                        "FAILED",
                        "COMPLETED",
                    } or not st:
                        chosen_id = str(cand["JOB_ID"])
                        job = cand
                        break
            if not chosen_id:
                job = store.create_job(
                    total=prior_total or 0,
                    opportunity_ids=[],
                    meta={
                        "build": BUILD,
                        "slice_size": SLICE_SIZE,
                        "AUTO_SEND": False,
                        "queue_pending": True,
                    },
                )
                chosen_id = str(job["JOB_ID"])

        job_id = chosen_id
        if retry_technical_failures:
            retry_report = requeue_retryable_failures(job_id)

        worker_id = f"fspf-{uuid.uuid4().hex[:10]}"
        try:
            store.acquire_lease(job_id, worker_id, stale_seconds=STALE_LEASE_S, force=True if force or retry_technical_failures else force)
        except JobLeaseError as exc:
            return {
                "ok": False,
                "status": "LEASE_HELD",
                "error": str(exc),
                "build": BUILD,
                "progress": job_progress(),
            }

        _stop_requested = False
        _save(STOP_FILE, {"stop": False})
        _progress(
            status="RUNNING",
            phase="QUEUED",
            job_id=job_id,
            worker_id=worker_id,
            eligible_total=prior_total or None,
            FINAL_STATUS=None,
            error=None,
            retry_report=retry_report,
        )

        def _run() -> None:
            global _worker
            try:
                _progress(
                    status="RUNNING",
                    phase="BUILD_QUEUE",
                    job_id=job_id,
                    worker_id=worker_id,
                )
                # Default: reuse existing queue. Never rebuild during emergency resume.
                queue = build_eligible_queue(
                    force_rebuild=rebuild_queue,
                    job_id=job_id,
                    worker_id=worker_id,
                )
                oids = [str(r["opportunity_id"]) for r in (queue.get("rows") or [])]
                job_now = store.load_job(job_id) or {}
                job_now["TOTAL"] = len(oids)
                # Persist oid list separately when huge — keep job.json lean
                if len(oids) <= 500:
                    job_now["OPPORTUNITY_IDS"] = oids
                else:
                    job_now["OPPORTUNITY_IDS"] = oids[:20]
                    job_now["OPPORTUNITY_IDS_NOTE"] = "truncated; full list in QUEUE_FILE"
                job_now["META"] = {
                    **(job_now.get("META") or {}),
                    "build": BUILD,
                    "slice_size": SLICE_SIZE,
                    "AUTO_SEND": False,
                    "queue_source": queue.get("source"),
                    "eligible_total": queue.get("eligible_total"),
                    "queue_pending": False,
                }
                store.save_job(job_now)
                _progress(
                    status="RUNNING",
                    phase="QUEUE_READY",
                    job_id=job_id,
                    worker_id=worker_id,
                    eligible_total=len(oids),
                )
                run_continuous(job_id=job_id, worker_id=worker_id, slice_size=SLICE_SIZE)
            except Exception as exc:
                log.exception("full-scale production funnel failed")
                try:
                    store.release_lease(job_id, worker_id, final_status="ERROR")
                except Exception:
                    pass
                _progress(
                    status="ERROR",
                    phase="FAILED",
                    error=f"{type(exc).__name__}:{exc}"[:240],
                    job_id=job_id,
                    finished_at=_utc(),
                )
            finally:
                _worker = False

        _worker = True
        threading.Thread(target=_run, name=f"{FAMILY}-{worker_id}", daemon=True).start()
        return {
            "ok": True,
            "status": "STARTED",
            "build": BUILD,
            "job_id": job_id,
            "worker_id": worker_id,
            "eligible_total": prior_total or None,
            "eligible_total_pending": prior_total == 0 or bool(new_run),
            "slice_size": SLICE_SIZE,
            "AUTO_SEND": False,
            "retry_technical_failures": bool(retry_technical_failures),
            "retry_report": retry_report,
            "new_run": bool(new_run),
            "archive_report": archive_report,
            "progress": job_progress(),
        }


def recent_item_samples(*, limit: int = 25) -> dict[str, Any]:
    """Return recent checkpoints for diagnosis — no secrets."""
    store = JobStore(FAMILY)
    active = store.find_active_job(stale_seconds=3600)
    if not active:
        return {"status": "NO_JOB", "build": BUILD, "items": []}
    jid = str(active["JOB_ID"])
    items = store.list_items(jid)
    # Prefer most recently checkpointed
    items.sort(key=lambda x: str(x.get("CHECKPOINT_AT") or ""), reverse=True)
    slim = []
    for it in items[: max(1, min(limit, 50))]:
        slim.append(
            {
                "opportunity_id": it.get("OPPORTUNITY_ID"),
                "RESULT": it.get("RESULT"),
                "terminal_blocker": it.get("terminal_blocker"),
                "PREVIOUS_BLOCKER": it.get("PREVIOUS_BLOCKER"),
                "RETRY_COUNT": it.get("RETRY_COUNT"),
                "fetch_via": it.get("fetch_via"),
                "is_bidnet_url": it.get("is_bidnet_url"),
                "authoritative_url": it.get("authoritative_url"),
                "purchasing_group_id": it.get("purchasing_group_id")
                or _pg_id_from_url(it.get("authoritative_url")),
                "purchasing_group_registration_status": it.get(
                    "purchasing_group_registration_status"
                ),
                "portal_account": it.get("portal_account"),
                "document_count": it.get("document_count"),
                "documents_downloaded": it.get("documents_downloaded"),
                "line_count": it.get("line_count"),
                "fully_specified_product_lines": it.get("fully_specified_product_lines"),
                "complete_spec": it.get("complete_spec"),
                "access_note": it.get("access_note"),
                "early_search_status": it.get("early_search_status"),
                "search_to_detail_status": it.get("search_to_detail_status"),
                "bidnet_solicitation_id": it.get("bidnet_solicitation_id"),
                "bounded_blocker": it.get("bounded_blocker"),
                "CHECKPOINT_AT": it.get("CHECKPOINT_AT"),
            }
        )
    return {"status": "OK", "build": BUILD, "job_id": jid, "count": len(slim), "items": slim}


def get_scorecard() -> dict[str, Any]:
    sc = _load(SCORECARD_FILE)
    if sc:
        sc["build"] = BUILD
        sc["progress"] = job_progress()
        return sc
    queue = _load(QUEUE_FILE) or {}
    return {
        "kind": "FullScaleProductionFunnelScorecard",
        "build": BUILD,
        "status": "NO_SCORECARD_YET",
        "metrics": {
            "live_eligible_product_opportunities": queue.get("eligible_total") or 0,
            "opportunities_processed": 0,
            "complete_solicitation_packages": 0,
            "documents_retrieved": 0,
            "fully_specified_product_lines": 0,
            "complete_bidding_lots": 0,
            "matched_distributors": 0,
            "verified_public_acquisition_prices": 0,
            "verified_historical_government_prices": 0,
            "complete_rfqs_awaiting_approval": 0,
            "real_supplier_quotes_received": 0,
            "documented_positive_margins": 0,
            "financing_ready_opportunities": 0,
            "fully_verified_bid_ready_opportunities": 0,
        },
        "progress": job_progress(),
        "api_budget": _budget_snapshot(),
    }
