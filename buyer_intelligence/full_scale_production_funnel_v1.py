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

BUILD = "20261015-m3-full-scale-production-funnel-v4e6"
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
STALE_LEASE_S = float(
    os.environ.get("M3_FSPF_STALE_LEASE_S")
    or LONG_RUNNING_JOB_EXECUTION_STANDARD.get("STALE_RUNNING_SECONDS")
    or 120
)
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
) -> None:
    """Append to back-of-line deferred queue (idempotent per oid)."""
    q = _load_deferred_queue()
    items = [x for x in (q.get("items") or []) if str(x.get("opportunity_id")) != opportunity_id]
    items.append(
        {
            "opportunity_id": opportunity_id,
            "stage_reached": stage,
            "funnel_outcome": outcome,
            "software_defect_key": defect_key,
            "shared_access_key": shared_access_key,
            "terminal_blocker": (blocker or "")[:160] or None,
            "queue_index": index,
            "deferred_at": _utc(),
        }
    )
    q["items"] = items[-20000:]
    by_def = dict(q.get("by_defect") or {})
    if defect_key:
        by_def[defect_key] = int(by_def.get(defect_key) or 0) + 1
    q["by_defect"] = by_def
    q["updated_at"] = _utc()
    q["build"] = BUILD
    q["count"] = len(q["items"])
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
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
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
    cur.update(kwargs)
    cur["build"] = BUILD
    cur["heartbeat_at"] = _utc()
    cur["AUTO_SEND"] = AUTO_SEND
    _save(PROGRESS_FILE, cur)
    return cur


def job_progress() -> dict[str, Any]:
    prog = _load(PROGRESS_FILE) or {"status": "IDLE", "build": BUILD}
    store = JobStore(FAMILY)
    active = store.find_active_job(stale_seconds=STALE_LEASE_S)
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


def release_pg_blocked_for_registered_groups(job_id: str) -> dict[str, Any]:
    """Mark deferred/blocked opportunities eligible for recovery after PG REGISTERED.

    Does NOT reset RESUME_CURSOR — first-pass continues uninterrupted. Matching
    deferred-queue items become recovery-eligible and are resumed after first-pass
    (or when the deferred lane runs). Exact stage checkpoints are preserved.
    """
    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url
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
    deferred_marked = 0
    for it in store.list_items(job_id):
        if not isinstance(it, dict):
            continue
        examined += 1
        blocker = str(it.get("terminal_blocker") or it.get("TERMINAL_BLOCKER") or "")
        result = str(it.get("RESULT") or "")
        sak = str(it.get("shared_access_key") or "")
        if result in _PRESERVE_RESULTS and result != "OWNER_ACTION_REQUIRED":
            continue
        pg = str(it.get("purchasing_group_id") or "") or extract_pg_id_from_url(
            str(it.get("authoritative_url") or "")
        )
        if not pg and sak.startswith("pg:"):
            pg = sak.split(":", 1)[1].strip()
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
        if not blocker_ok and not result_ok:
            continue
        it["PREVIOUS_RESULT"] = result
        it["PREVIOUS_BLOCKER"] = blocker
        it["RETRYABLE"] = True
        it["RESULT"] = "PENDING_RETRY"
        it["RETRY_QUEUED_AT"] = _utc()
        it["NOTE"] = f"Released after purchasing group {pg} REGISTERED"
        it["purchasing_group_id"] = pg
        it["PG_UNLOCK_ELIGIBLE"] = True
        it["recovery_eligible"] = True
        # Preserve interrupted stage for deferred resume — do not wipe checkpoints
        # Reset retry budget once for post-registration unlock
        if not it.get("PG_UNLOCK_RETRY"):
            it["PG_UNLOCK_RETRY"] = True
            it["RETRY_COUNT"] = 0
        store.write_item_checkpoint(job_id, it)
        marked += 1

    # Flag matching deferred-queue rows for post-first-pass recovery
    try:
        dq = _load_deferred_queue()
        items = list(dq.get("items") or [])
        changed = False
        for d in items:
            if not isinstance(d, dict):
                continue
            sak = str(d.get("shared_access_key") or "")
            blocker = str(d.get("terminal_blocker") or "")
            pg = ""
            if sak.startswith("pg:"):
                pg = sak.split(":", 1)[1].strip()
            if not pg and "PURCHASING" in blocker.upper():
                pg = extract_pg_id_from_url(str(d.get("authoritative_url") or "")) or ""
            if not pg or (pg not in registered and not is_registered(pg)):
                continue
            if d.get("recovery_eligible") and d.get("pg_unlocked"):
                continue
            d["recovery_eligible"] = True
            d["pg_unlocked"] = pg
            d["unlocked_at"] = _utc()
            deferred_marked += 1
            changed = True
        if changed:
            dq["items"] = items
            dq["updated_at"] = _utc()
            dq["pg_unlock_eligible_count"] = sum(
                1 for x in items if isinstance(x, dict) and x.get("recovery_eligible")
            )
            _save(DEFERRED_QUEUE_FILE, dq)
    except Exception:
        pass

    if marked or deferred_marked:
        job = store.load_job(job_id) or job
        # Do NOT reset RESUME_CURSOR — that would interrupt unfinished first-pass work.
        meta = dict(job.get("META") or {})
        meta["pg_unlock_released"] = int(meta.get("pg_unlock_released") or 0) + marked
        meta["pg_unlock_deferred_marked"] = int(meta.get("pg_unlock_deferred_marked") or 0) + deferred_marked
        meta["pg_unlock_at"] = _utc()
        meta["repair_build"] = BUILD
        job["META"] = meta
        store.save_job(job)
        store.recompute_counts(job_id)
    return {
        "job_id": job_id,
        "registered_groups": sorted(registered),
        "examined": examined,
        "released": marked,
        "deferred_marked": deferred_marked,
        "build": BUILD,
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


def _is_active_lease_worker(job_id: str, worker_id: str) -> bool:
    """True only while this worker still holds the exclusive job lease.

    Prevents zombie threads (from force-restart) from continuing after a newer
    worker acquires the lease — clearing _stop_requested must not revive them.
    """
    try:
        job = JobStore(FAMILY).load_job(job_id) or {}
    except Exception:
        return False
    if str(job.get("WORKER_ID") or "") != str(worker_id or ""):
        return False
    return str(job.get("STATUS") or "") in {"RUNNING", "CREATED"}


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
    """Compact JSON (no indent) — 10k-row queues must stay writable under memory pressure."""
    path = Path(data_path(QUEUE_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(man, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, path)
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
            if isinstance(deals, list) and deals:
                rows_in = [d for d in deals if isinstance(d, dict)]
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
    total = int(job.get("TOTAL") or queue.get("eligible_total") or 0)
    scorecard = {
        "kind": "FullScaleProductionFunnelScorecard",
        "build": BUILD,
        "at": _utc(),
        "AUTO_SEND": AUTO_SEND,
        "job_id": job.get("JOB_ID"),
        "worker_id": job.get("WORKER_ID"),
        "status": job.get("STATUS") or "UNKNOWN",
        "heartbeat_at": job.get("HEARTBEAT_AT"),
        "last_checkpoint_at": job.get("LAST_CHECKPOINT_AT"),
        "current_opportunity_id": job.get("CURRENT_OPPORTUNITY_ID"),
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

    queue = _load(QUEUE_FILE) or build_eligible_queue()
    rows = queue.get("rows") or []
    job, pass_mode, first_pass, cursor = _heal_false_first_pass_exhaustion(store, job, rows)
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

    if first_pass:
        pending, registered_pending, _scan_end = _collect_first_pass_pending(
            store,
            job_id,
            rows,
            cursor=cursor,
            slice_size=slice_size,
            registered_pgs=registered_pgs,
        )
        if not pending:
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
        # Second pass: resume deferred errors at exact stage (oldest first)
        if not (_load_deferred_queue().get("items")):
            _backfill_deferred_from_attempted(store, job_id, rows)
        dq = _load_deferred_queue()
        deferred_items = list(dq.get("items") or [])
        pending = []
        for d in deferred_items[: slice_size * 3]:
            oid = str(d.get("opportunity_id") or "")
            if not oid:
                continue
            prev = store.load_item(job_id, oid)
            if prev and str(prev.get("RESULT") or "") in _PRESERVE_RESULTS:
                continue
            if _component_circuit_tripped(d.get("software_defect_key")):
                continue
            idx = int(d.get("queue_index") or 0)
            item = rows[idx] if 0 <= idx < len(rows) else {"opportunity_id": oid}
            if str(item.get("opportunity_id") or "") != oid:
                item = {"opportunity_id": oid}
                for j, r in enumerate(rows):
                    if str(r.get("opportunity_id")) == oid:
                        item = r
                        idx = j
                        break
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
        store.write_item_checkpoint(job_id, slim)
        _append_rfq(row)
        if row.get("defer") or result in {"DEFERRED_ERROR", "DEFERRED_EXTERNAL", "TIMEOUT"}:
            _defer_opportunity(
                opportunity_id=oid_local,
                stage=row.get("stage_reached"),
                outcome=str(row.get("funnel_outcome") or result),
                defect_key=row.get("software_defect_key"),
                shared_access_key=row.get("shared_access_key"),
                blocker=row.get("terminal_blocker"),
                index=idx,
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
        store.heartbeat(
            job_id,
            worker_id=worker_id,
            current_index=idx,
            current_opportunity_id=oid_local,
        )
        return result

    # --- Fast path: no browser ---
    for idx, item, access in fast_pending:
        if _stop_flag() or not _is_active_lease_worker(job_id, worker_id):
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

    browser_slot_held = False
    try:
        if browser_pending:
            from bidnet_auth.browser_mutex_v1 import (
                release_bidnet_browser,
                try_acquire_bidnet_browser,
            )

            # Opportunity lane owns Playwright while browser work runs; PG manager waits.
            browser_slot_held = try_acquire_bidnet_browser(
                holder=f"fspf-{worker_id}", timeout_s=90.0
            )
            if not browser_slot_held:
                _progress(
                    status="RUNNING",
                    phase="WAIT_PG_BROWSER_SLOT",
                    job_id=job_id,
                    worker_id=worker_id,
                    note="pg_access_manager_holds_playwright_deferring_browser_slice",
                )
                # Do not block the whole slice forever — return and retry next tick
                return {
                    "slice_complete": True,
                    "processed_this_slice": processed,
                    "queue_exhausted": False,
                    "job_id": job_id,
                    "browser_slot_deferred": True,
                    "pg_group_slice": pg_slice_report,
                    "timing_accum": timing_accum,
                }
            auth_status, auth_client = _open_bidnet_auth_client()
            if not auth_status.get("authenticated"):
                _progress(
                    status="BLOCKED",
                    phase="AUTH_BLOCKER",
                    last_blocker=auth_status.get("external_blocker") or auth_status.get("status"),
                    job_id=job_id,
                    worker_id=worker_id,
                    bidnet_auth_status=auth_status.get("status"),
                    auth_message=(auth_status.get("message") or "")[:200],
                    timing_accum=timing_accum,
                )
                return {
                    "slice_complete": False,
                    "processed_this_slice": processed,
                    "queue_exhausted": False,
                    "job_id": job_id,
                    "blocked": True,
                    "reason": "BIDNET_AUTH_REQUIRED",
                    "auth_status": auth_status,
                    "pg_group_slice": pg_slice_report,
                    "timing_accum": timing_accum,
                }
        for idx, item in browser_pending:
            if _stop_flag() or not _is_active_lease_worker(job_id, worker_id):
                break
            oid = str(item["opportunity_id"])
            last_oid = oid
            store.heartbeat(
                job_id,
                worker_id=worker_id,
                current_index=idx,
                current_opportunity_id=oid,
            )
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
                # Recreate auth client if prior timeout/exception closed it
                if auth_client is None or not getattr(auth_client, "is_authenticated", False):
                    if auth_client is not None:
                        try:
                            auth_client.close()
                        except Exception:
                            pass
                    auth_status, auth_client = _open_bidnet_auth_client()

                # ONE continuous journey with hard wall-clock so hung Playwright
                # cannot strand the worker past ITEM_TIMEOUT_S.
                from concurrent.futures import ThreadPoolExecutor
                from concurrent.futures import TimeoutError as FuturesTimeout

                def _run() -> dict[str, Any]:
                    return run_one_opportunity(
                        item,
                        client=http,
                        evidence_index=evidence_index,
                        auth_status=auth_status,
                        l23_rec=l23.get(oid) or {},
                        auth_client=auth_client,
                    )

                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(_run)
                    try:
                        row = fut.result(timeout=ITEM_TIMEOUT_S)
                    except FuturesTimeout:
                        try:
                            if auth_client is not None:
                                auth_client.close()
                        except Exception:
                            pass
                        auth_client = None
                        row = {
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
                        }
                # Shared client may be poisoned by Playwright target-closed errors
                note = str(row.get("access_note") or "")
                early_st = str(
                    ((row.get("early_search_to_detail") or {}).get("status") or "")
                )
                if (
                    "recover_exception" in note
                    or early_st.startswith("SEARCH_EXCEPTION")
                    or early_st == "RECOVER_EXCEPTION"
                    or "TargetClosed" in note
                    or "TargetClosed" in early_st
                ):
                    try:
                        auth_client.close()
                    except Exception:
                        pass
                    auth_client = None
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
        price_pass: dict[str, Any] | None = None
        # Skip expensive end-of-slice price/PG browser work when this slice only
        # performed shared-access fast deferrals (no new specs to price).
        slice_all_fast = (
            int(timing_accum.get("fast_path_count") or 0) > 0
            and int(timing_accum.get("browser_path_count") or 0) == 0
        )
        try:
            if _is_active_lease_worker(job_id, worker_id) and not slice_all_fast:
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
            }
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
        last_opportunity_id=last_oid,
        scorecard_metrics=scorecard.get("metrics"),
    )
    return {
        "slice_complete": True,
        "processed_this_slice": processed,
        "queue_exhausted": int(job.get("COMPLETED") or 0) >= int(job.get("TOTAL") or 0),
        "job_id": job_id,
        "completed": job.get("COMPLETED"),
        "total": job.get("TOTAL"),
        "scorecard": scorecard.get("metrics"),
    }


def run_continuous(*, job_id: str, worker_id: str, slice_size: int = SLICE_SIZE) -> dict[str, Any]:
    """Run slice after slice until queue exhausted, stop, or hard blocker."""
    store = JobStore(FAMILY)
    # Independent PG access lane — separate browser, never blocks opportunity workers.
    try:
        from bidnet_auth.purchasing_group_access_manager_v1 import ensure_access_manager_running

        ensure_access_manager_running(job_id=job_id)
    except Exception as exc:
        log.warning("pg access manager start failed: %s", type(exc).__name__)
    slices = 0
    while True:
        if not _is_active_lease_worker(job_id, worker_id):
            _progress(
                status="STOPPED",
                phase="LEASE_SUPERSEDED",
                job_id=job_id,
                worker_id=worker_id,
                note="exiting_zombie_worker",
            )
            break
        if _stop_flag():
            # Only the lease holder may release / cancel the job.
            if _is_active_lease_worker(job_id, worker_id):
                store.release_lease(job_id, worker_id, final_status="CANCELLED")
                _progress(status="CANCELLED", phase="STOPPED", job_id=job_id)
            break
        out = process_slice(job_id=job_id, worker_id=worker_id, slice_size=slice_size)
        slices += 1
        if not _is_active_lease_worker(job_id, worker_id):
            _progress(
                status="STOPPED",
                phase="LEASE_SUPERSEDED",
                job_id=job_id,
                worker_id=worker_id,
            )
            break
        store.heartbeat(job_id, worker_id=worker_id)
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
        }:
            # Resume original production job even after stop/error — do not create a parallel worker job
            chosen_id = str(active["JOB_ID"])
            job = store.load_job(chosen_id) or active
        else:
            # Scan newest for the original family job before creating
            chosen_id = None
            for p in sorted(store.root.glob("*/job.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
                cand = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
                if isinstance(cand, dict) and cand.get("FAMILY") == FAMILY:
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
