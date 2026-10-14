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

BUILD = "20261013-m3-full-scale-production-funnel-v1"
FAMILY = "full_scale_production_funnel_v1"
OUT = "_og_commercial_research_pipeline_out"

FCF_CLASS = f"{OUT}/m3_fcf_v1_classifications.json"
FCF_SCORE = f"{OUT}/m3_fcf_v1_scorecard.json"
QUEUE_FILE = f"{OUT}/m3_fspf_v1_eligible_queue.json"
PROGRESS_FILE = f"{OUT}/m3_fspf_v1_progress.json"
SCORECARD_FILE = f"{OUT}/m3_fspf_v1_scorecard.json"
RFQ_QUEUE_FILE = f"{OUT}/m3_fspf_v1_rfq_queue.json"
STOP_FILE = f"{OUT}/m3_fspf_v1_stop.json"
REPORT_FILE = f"{OUT}/m3_fspf_v1_report.json"

SLICE_SIZE = int(os.environ.get("M3_FSPF_SLICE_SIZE") or "25")
ITEM_TIMEOUT_S = float(os.environ.get("M3_FSPF_ITEM_TIMEOUT_S") or "50")
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


def request_stop() -> dict[str, Any]:
    global _stop_requested
    _stop_requested = True
    _save(STOP_FILE, {"stop": True, "requested_at": _utc()})
    return {"status": "STOP_REQUESTED", "build": BUILD, "progress": job_progress()}


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


def build_eligible_queue(*, force_rebuild: bool = False) -> dict[str, Any]:
    """Build prioritized full LIVE_ELIGIBLE product queue from FCF classifications.

    No artificial total cap. If classifications are missing, rebuild a live-eligible
    product queue from L23 via FCF classify_record (same taxonomy).
    """
    existing = None if force_rebuild else _load(QUEUE_FILE)
    if (
        existing
        and isinstance(existing.get("rows"), list)
        and existing.get("eligible_total", 0) > 0
        and not force_rebuild
    ):
        return existing

    cls = _load(FCF_CLASS) or {}
    rows_in = cls.get("rows") or []
    source = "fcf_classifications"

    if not rows_in:
        # Rebuild from L23 using FCF classifier (no permanent raw warehouse expansion)
        from buyer_intelligence.full_cohort_funnel_run_v1 import (
            LIVE_ELIGIBLE,
            classify_record,
            map_terminal_class,
        )
        from phase_l.l23_full_population_funnel import load_store

        store = load_store()
        rebuilt: list[dict[str, Any]] = []
        for oid, rec in (store or {}).items():
            if not isinstance(rec, dict):
                continue
            classified = classify_record(str(oid), rec)
            fclass = map_terminal_class(classified, None)
            if fclass != LIVE_ELIGIBLE:
                continue
            if classified.get("product_class") not in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
                continue
            classified["funnel_class"] = fclass
            classified["opportunity_id"] = str(oid)
            rebuilt.append(classified)
        rows_in = rebuilt
        source = "l23_classify_fallback"
        log.warning("FCF classifications missing — built eligible queue from L23 (%s)", len(rows_in))

    selected: list[dict[str, Any]] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        if r.get("funnel_class") != "LIVE_ELIGIBLE":
            continue
        if r.get("product_class") not in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
            continue
        # Soft-skip already-confirmed closed when classification is stale
        if r.get("deadline_status") in {"CLOSED_CONFIRMED", "CANCELLED"}:
            continue
        title = str(r.get("product") or r.get("title") or "")
        if _COMPLEX.search(title) and not _COMMON.search(title):
            continue
        oid = str(r.get("opportunity_id") or "").strip()
        if not oid:
            continue
        selected.append(
            {
                "opportunity_id": oid,
                "title": title[:240],
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "days_left": r.get("days_left"),
                "authoritative_url": r.get("authoritative_url"),
                "product_class": r.get("product_class"),
                "funnel_class": r.get("funnel_class"),
                "deadline_status": r.get("deadline_status"),
                "common_product": bool(r.get("common_product") or _COMMON.search(title)),
                "selection_score": _priority_score(r),
            }
        )

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
    _save(QUEUE_FILE, man)
    return man


def _map_result(row: dict[str, Any]) -> str:
    blocker = str(row.get("terminal_blocker") or row.get("package_blocker") or "")
    if row.get("timed_out") or row.get("bounded_timed_out") or "TIMEOUT" in blocker:
        return "TIMEOUT"
    if "CREDENTIAL" in blocker or "AUTH" in blocker:
        return "AUTH_FAILED"
    if blocker in {
        "SOURCE_REQUIRES_PAID_ENTITLEMENT",
        "SOURCE_REQUIRES_PAID_ENTITLEMENT_OR_LOGIN",
        "PURCHASING_GROUP_REGISTRATION_REQUIRED",
        "AWAITING_OWNER_APPROVED_OUTREACH",
    } or blocker.startswith("PAID_ENTITLEMENT"):
        if "OWNER" in blocker or blocker == "AWAITING_OWNER_APPROVED_OUTREACH":
            return "OWNER_ACTION_REQUIRED"
        return "NO_PACKAGE" if not row.get("complete_spec") else "PARTIAL"
    if row.get("complete_spec") and (row.get("document_count") or 0) > 0:
        return "SUCCESS"
    if row.get("complete_spec") or (row.get("line_count") or 0) > 0:
        return "PARTIAL"
    if row.get("package_status") in {"DOCUMENTS_RECOVERED", "FREE_PACKAGE_FOUND", "AUTHENTICATED_PACKAGE_DOCS"}:
        return "PARTIAL"
    if "EXPIRED" in blocker or row.get("deadline_status") == "CLOSED_CONFIRMED":
        return "EXPIRED_EXIT"
    if row.get("fetch_error") or row.get("process_bidnet_error"):
        return "FETCH_FAILED"
    return "NO_PACKAGE"


def _slim_checkpoint(row: dict[str, Any], *, result: str, index: int) -> dict[str, Any]:
    rfq = row.get("rfq") if isinstance(row.get("rfq"), dict) else {}
    econ = row.get("economics") if isinstance(row.get("economics"), dict) else {}
    docs = row.get("recovered_documents") or []
    lines = row.get("line_items") or []
    fully_specified = 0
    for li in lines:
        if not isinstance(li, dict):
            continue
        if li.get("source") == "title_fallback":
            continue
        qty = li.get("quantity")
        if isinstance(li.get("QTY"), dict):
            qty = qty or (li.get("QTY") or {}).get("VALUE")
        desc = li.get("description") or li.get("DESCRIPTION")
        if qty not in (None, "") and desc:
            fully_specified += 1
    return {
        "OPPORTUNITY_ID": row.get("opportunity_id"),
        "RESULT": result,
        "INDEX": index,
        "title": (row.get("title") or "")[:160],
        "buyer": row.get("buyer"),
        "deadline": row.get("deadline"),
        "package_status": row.get("package_status"),
        "terminal_blocker": row.get("terminal_blocker") or row.get("package_blocker"),
        "document_count": int(row.get("document_count") or len(docs) or 0),
        "documents_downloaded": int(
            row.get("documents_downloaded")
            or sum(1 for d in docs if isinstance(d, dict) and (d.get("local_path_set") or d.get("retrieval_status")))
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
        "rfq_status": rfq.get("status"),
        "rfq_ready_to_send": False,
        "econ_state": row.get("econ_state") or econ.get("econ_state"),
        "financeable": False,
        "bid_ready": False,
        "elapsed_s": row.get("elapsed_s"),
        "fetch_via": row.get("fetch_via"),
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
        if it.get("public_unit_price") not in (None, ""):
            public_prices += 1
        if it.get("historical_unit_price") not in (None, ""):
            hist_prices += 1
        if it.get("rfq_status") == "AWAITING_OWNER_APPROVED_OUTREACH":
            rfqs += 1
        if it.get("econ_state") in {
            "PROFITABLE_BUT_QUOTE_MISSING",
            "PROFITABLE_BUT_FINANCING_UNVERIFIED",
            "VERIFIED_PROFITABLE_AND_FINANCEABLE",
        }:
            positive_margins += 1

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
            "complete_solicitation_packages": packages,
            "documents_retrieved": docs,
            "fully_specified_product_lines": fully,
            "complete_bidding_lots": sum(1 for it in items if it.get("complete_spec")),
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


def process_slice(
    *,
    job_id: str,
    worker_id: str,
    slice_size: int = SLICE_SIZE,
) -> dict[str, Any]:
    """Process the next slice of incomplete opportunities; checkpoint each item."""
    from buyer_intelligence.commercial_evidence_index_v1 import build_commercial_evidence_index
    from buyer_intelligence.commercial_funnel_execution_v1 import (
        _cookies_client,
        _open_bidnet_auth_client,
        process_one,
    )
    from phase_l.l23_full_population_funnel import load_store

    store = JobStore(FAMILY)
    job = store.load_job(job_id)
    if not job:
        raise KeyError(job_id)

    queue = _load(QUEUE_FILE) or build_eligible_queue()
    rows = queue.get("rows") or []
    cursor = int(job.get("RESUME_CURSOR") or 0)

    # Skip already-checkpointed
    pending: list[tuple[int, dict[str, Any]]] = []
    for i in range(cursor, len(rows)):
        item = rows[i]
        oid = str(item.get("opportunity_id") or "")
        prev = store.load_item(job_id, oid)
        if prev and prev.get("RESULT") in _TERMINAL:
            continue
        pending.append((i, item))
        if len(pending) >= slice_size:
            break

    if not pending:
        job["RESUME_CURSOR"] = len(rows)
        store.save_job(job)
        return {
            "slice_complete": True,
            "processed_this_slice": 0,
            "queue_exhausted": True,
            "job_id": job_id,
        }

    _progress(
        status="RUNNING",
        phase="SLICE",
        job_id=job_id,
        worker_id=worker_id,
        slice_pending=len(pending),
        resume_cursor=cursor,
        eligible_total=len(rows),
    )

    evidence_index = build_commercial_evidence_index()
    auth_status, auth_client = _open_bidnet_auth_client()
    l23 = load_store()
    http = _cookies_client()
    processed = 0
    last_oid = None
    try:
        for idx, item in pending:
            if _stop_flag():
                break
            oid = str(item["opportunity_id"])
            last_oid = oid
            store.heartbeat(
                job_id,
                worker_id=worker_id,
                current_index=idx,
                current_opportunity_id=oid,
            )
            _progress(
                status="RUNNING",
                phase="PROCESS_ONE",
                job_id=job_id,
                worker_id=worker_id,
                current_index=idx,
                current_opportunity_id=oid,
                completed=job.get("COMPLETED"),
                eligible_total=len(rows),
            )

            # Early exit for expired / cancelled before expensive recovery
            try:
                days = item.get("days_left")
                if days is not None and float(days) < 0:
                    slim = {
                        "OPPORTUNITY_ID": oid,
                        "RESULT": "EXPIRED_EXIT",
                        "INDEX": idx,
                        "terminal_blocker": "DEADLINE_PASSED_REVALIDATION",
                        "CHECKPOINT_AT": _utc(),
                    }
                    store.write_item_checkpoint(job_id, slim)
                    processed += 1
                    job = store.recompute_counts(job_id)
                    job["RESUME_CURSOR"] = idx + 1
                    store.save_job(job)
                    continue
            except Exception:
                pass

            t0 = time.time()
            try:
                # Recreate auth client if prior timeout closed it
                if auth_client is None or not getattr(auth_client, "is_authenticated", False):
                    if auth_client is not None:
                        try:
                            auth_client.close()
                        except Exception:
                            pass
                    auth_status, auth_client = _open_bidnet_auth_client()

                row = process_one(
                    item,
                    client=http,
                    evidence_index=evidence_index,
                    auth_status=auth_status,
                    l23_rec=l23.get(oid) or {},
                    auth_client=auth_client,
                )
                # Soft wall: if process_one exceeded budget, mark timeout
                if time.time() - t0 > ITEM_TIMEOUT_S and not row.get("complete_spec"):
                    row["bounded_timed_out"] = True
                    row["terminal_blocker"] = row.get("terminal_blocker") or "PER_OPPORTUNITY_TIMEOUT"
            except Exception as exc:
                row = {
                    "opportunity_id": oid,
                    "title": item.get("title"),
                    "terminal_blocker": f"PROCESS_EXCEPTION:{type(exc).__name__}",
                    "process_error": str(exc)[:200],
                    "elapsed_s": round(time.time() - t0, 2),
                }
                log.exception("FSPF process_one failed oid=%s", oid)

            result = _map_result(row)
            slim = _slim_checkpoint(row, result=result, index=idx)
            store.write_item_checkpoint(job_id, slim)
            _append_rfq(row)
            processed += 1
            job = store.recompute_counts(job_id)
            job["RESUME_CURSOR"] = idx + 1
            job["META"] = {
                **(job.get("META") or {}),
                "last_blocker": slim.get("terminal_blocker"),
                "last_result": result,
                "bidnet_auth_status": auth_status.get("status"),
            }
            store.save_job(job)

            if SOURCE_COOLDOWN_S > 0:
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
    slices = 0
    while True:
        if _stop_flag():
            store.release_lease(job_id, worker_id, final_status="CANCELLED")
            _progress(status="CANCELLED", phase="STOPPED", job_id=job_id)
            break
        out = process_slice(job_id=job_id, worker_id=worker_id, slice_size=slice_size)
        slices += 1
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


def start_job(*, force: bool = False, rebuild_queue: bool = False) -> dict[str, Any]:
    """Start (or resume) the full-scale funnel as a background daemon thread.

    Returns immediately — queue build + processing run in the worker thread so
    the HTTP start endpoint never blocks on the 10k+ eligible population.
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
            if not force and not stale:
                return {"status": "ALREADY_RUNNING", "build": BUILD, "progress": cur}

        store = JobStore(FAMILY)
        active = store.find_active_job(stale_seconds=STALE_LEASE_S)
        existing_queue = None if rebuild_queue else _load(QUEUE_FILE)
        prior_total = int((existing_queue or {}).get("eligible_total") or 0)

        if active and active.get("STATUS") in {"RUNNING", "CREATED", "INTERRUPTED", "STALE_RELEASED"}:
            job_id = str(active["JOB_ID"])
            job = store.load_job(job_id) or active
        else:
            # Placeholder job — TOTAL refreshed after queue build in worker
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
            job_id = str(job["JOB_ID"])

        worker_id = f"fspf-{uuid.uuid4().hex[:10]}"
        try:
            store.acquire_lease(job_id, worker_id, stale_seconds=STALE_LEASE_S, force=force)
        except JobLeaseError as exc:
            return {
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
                queue = build_eligible_queue(force_rebuild=rebuild_queue)
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
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
        return {
            "status": "STARTED",
            "build": BUILD,
            "job_id": job_id,
            "worker_id": worker_id,
            "eligible_total": prior_total or None,
            "eligible_total_pending": prior_total == 0,
            "slice_size": SLICE_SIZE,
            "AUTO_SEND": False,
            "progress": job_progress(),
        }


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
