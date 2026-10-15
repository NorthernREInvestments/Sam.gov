"""Full L23 ↔ production-queue reconciliation and missing-opportunity admission.

Accounts for every physical L23 record by canonical opportunity ID.
Appends incorrectly excluded / unresolved IDs onto the EXISTING FSPF queue
without resetting RESUME_CURSOR or starting a competing worker.

Reconciliation is an inexpensive local scan (no Playwright / SAM). Long scans
run in a background thread so HTTP proxies do not 502.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.available_deals_visibility_v1 import (
    BLOCKED,
    NEEDS_VERIFICATION,
    READY,
    WORTH,
    _deadline_status,
    _is_expired,
    _product_class,
    classify_eligibility,
    classify_record,
)
from m3_data_root import data_path
from phase_l.l23_full_population_funnel import load_store

log = logging.getLogger("govtracker.l23_universe_reconciliation_v1")

BUILD = "20261015-m3-l23-universe-reconciliation-v1d"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_l23_universe_reconciliation_v1.json"
PROGRESS = f"{OUT}/m3_l23_universe_reconciliation_progress_v1.json"
APPEND_MANIFEST = f"{OUT}/m3_l23_universe_append_manifest_v1.json"
EXCLUSION_IDS = f"{OUT}/m3_l23_universe_exclusion_ids_v1.json"
FROZEN_MANIFEST_CANDIDATES = (
    f"{OUT}/m3_fcf_v1_run_manifest.json",
    f"{OUT}/m3_wddr_v1_frozen_manifest.json",
    f"{OUT}/m3_wddr_v1_frozen_manifest_ids_only.json",
    f"{OUT}/m3_wddr_v1_manifest.json",
    "m3_wddr_v1_frozen_manifest.json",
    "m3_wddr_v1_frozen_manifest_ids_only.json",
    "m3_fcf_v1_run_manifest.json",
)
QUEUE_FILE = f"{OUT}/m3_fspf_v1_eligible_queue.json"
ADV_DEALS = f"{OUT}/m3_adv_v1_available_deals.json"

_recon_lock = threading.Lock()
_recon_thread: threading.Thread | None = None

# Mutually exclusive physical-record categories (priority order applied in classify).
CAT_IN_QUEUE = "existing_production_queue"
CAT_ELIGIBLE_MISSING = "additional_unique_eligible"
CAT_UNRESOLVED = "additional_unresolved_needing_qualification"
CAT_EXPIRED = "confirmed_expired"
CAT_CANCELLED = "confirmed_cancelled"
CAT_DUPLICATE = "verified_duplicates_superseded"
CAT_INELIGIBLE = "definitively_ineligible"
CAT_OTHER = "other_evidenced_exclusions"


def _utc() -> str:
    return now_utc().isoformat()


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(rel: str, payload: Any) -> Path:
    """Write JSON durably. Prefer direct write — some volume mounts reject *.json.tmp rename."""
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"), default=str)
    path.write_text(raw, encoding="utf-8")
    return path


def _load_frozen_ids() -> tuple[list[str], str | None]:
    for rel in FROZEN_MANIFEST_CANDIDATES:
        man = _load(rel) or {}
        ids = man.get("ids") or man.get("frozen_ids") or []
        if isinstance(ids, list) and ids:
            return [str(x) for x in ids if str(x).strip()], rel
    # Bundled repo/image fallback (volume may lack the historical manifest).
    here = Path(__file__).resolve().parents[1]
    for rel in (
        here / "fixtures" / "m3_wddr_v1_frozen_manifest_ids_only.json",
        here / "data" / "_og_commercial_research_pipeline_out" / "m3_wddr_v1_frozen_manifest_ids_only.json",
        here / "data" / "_og_commercial_research_pipeline_out" / "m3_wddr_v1_frozen_manifest.json",
        here / "_og_commercial_research_pipeline_out" / "m3_wddr_v1_frozen_manifest_ids_only.json",
    ):
        try:
            if not rel.exists():
                continue
            man = json.loads(rel.read_text(encoding="utf-8"))
            ids = man.get("ids") or man.get("frozen_ids") or []
            if isinstance(ids, list) and ids:
                # Seed volume copy so subsequent reconcile/report paths see it.
                try:
                    _save(
                        f"{OUT}/m3_wddr_v1_frozen_manifest_ids_only.json",
                        {
                            "kind": "m3_wddr_v1_frozen_manifest",
                            "frozen": len(ids),
                            "ids": [str(x) for x in ids if str(x).strip()],
                            "seeded_from": str(rel),
                            "NOTE": "ids-only seed from image/repo backup",
                        },
                    )
                except Exception:
                    pass
                return [str(x) for x in ids if str(x).strip()], str(rel)
        except Exception:
            continue
    return [], None


def _queue_ids() -> set[str]:
    q = _load(QUEUE_FILE) or {}
    rows = q.get("rows") or []
    return {
        str(r.get("opportunity_id") or "").strip()
        for r in rows
        if isinstance(r, dict) and r.get("opportunity_id")
    }


def _adv_deal_ids() -> set[str]:
    adv = _load(ADV_DEALS) or {}
    deals = adv.get("deals") or []
    if isinstance(deals, dict):
        deals = list(deals.values())
    out: set[str] = set()
    for d in deals:
        if not isinstance(d, dict):
            continue
        oid = str(d.get("opportunity_id") or d.get("OPPORTUNITY_ID") or "").strip()
        if oid:
            out.add(oid)
    # Also count truncated needs_verification that were stored separately
    for d in adv.get("needs_verification_deals") or []:
        if isinstance(d, dict):
            oid = str(d.get("opportunity_id") or "").strip()
            if oid:
                out.add(oid)
    return out


def _category_for_classified(cl: dict[str, Any], *, in_queue: bool) -> tuple[str, str]:
    """Return (category, evidence_reason) for one classified L23 record."""
    if in_queue:
        return CAT_IN_QUEUE, "already_in_production_queue"

    bucket = str(cl.get("visibility_bucket") or "")
    block = str(cl.get("block_reason") or "")
    dl = str(cl.get("deadline_status") or "")
    pc = str(cl.get("product_class") or "")
    funnel = str(cl.get("funnel_state") or "").upper()
    elig = cl.get("eligible")

    # Duplicates / superseded (explicit store fields when present)
    if funnel in {"DUPLICATE", "SUPERSEDED", "AMENDED_SUPERSEDED"} or block in {
        "DUPLICATE",
        "SUPERSEDED",
    }:
        return CAT_DUPLICATE, f"funnel_or_block:{funnel or block}"

    if bucket == BLOCKED:
        if block == "CANCELLED" or dl == "CANCELLED" or funnel in {"CANCELED", "CANCELLED"}:
            return CAT_CANCELLED, block or "CANCELLED"
        if block == "EXPIRED" or dl == "CLOSED_CONFIRMED" or funnel == "EXPIRED":
            return CAT_EXPIRED, block or dl or "EXPIRED"
        if block == "NON_PRODUCT" or pc == "NON_PRODUCT":
            return CAT_INELIGIBLE, "NON_PRODUCT"
        if elig is False or block not in {None, "", "EXPIRED", "CANCELLED", "NON_PRODUCT"}:
            return CAT_INELIGIBLE, block or str(cl.get("eligibility_reason") or "INELIGIBLE")
        return CAT_OTHER, block or "BLOCKED_OTHER"

    if bucket == NEEDS_VERIFICATION or dl in {"DEADLINE_UNKNOWN", "DEADLINE_CONFLICT"}:
        return CAT_UNRESOLVED, dl or "NEEDS_VERIFICATION"

    if bucket in {WORTH, READY}:
        # Confirmed-open product not in queue → incorrectly filtered / dropped by ADV admission
        return CAT_ELIGIBLE_MISSING, f"adv_bucket_{bucket}_absent_from_queue"

    if pc in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
        return CAT_UNRESOLVED, f"unbucketed_product:{bucket or 'NONE'}"

    return CAT_OTHER, f"unclassified:{bucket or funnel or pc or 'NONE'}"


def _fast_gate_classify(oid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Deterministic visibility gates without commercial overlay / priority scoring."""
    elig = classify_eligibility(rec)
    pc = _product_class(rec)
    expired = _is_expired(rec)
    dl_status = _deadline_status(rec)
    funnel = str(rec.get("current_funnel_state") or "")

    if expired:
        bucket = BLOCKED
        block_reason = "EXPIRED" if dl_status != "CANCELLED" else "CANCELLED"
    elif pc == "NON_PRODUCT":
        bucket = BLOCKED
        block_reason = "NON_PRODUCT"
    elif elig.get("eligible") is False:
        bucket = BLOCKED
        block_reason = elig.get("reason") or "INELIGIBLE_SET_ASIDE"
    elif dl_status in {"DEADLINE_UNKNOWN", "DEADLINE_CONFLICT"}:
        bucket = NEEDS_VERIFICATION
        block_reason = dl_status
    elif funnel.upper() in {"DUPLICATE", "SUPERSEDED", "AMENDED_SUPERSEDED"}:
        bucket = BLOCKED
        block_reason = "DUPLICATE"
    else:
        bucket = WORTH
        block_reason = None

    return {
        "opportunity_id": oid,
        "product": rec.get("title"),
        "visibility_bucket": bucket,
        "block_reason": block_reason,
        "deadline_status": dl_status,
        "product_class": pc,
        "funnel_state": funnel,
        "eligible": elig.get("eligible"),
        "eligibility_reason": elig.get("reason"),
        "authoritative_url": rec.get("authoritative_url"),
        "deadline": rec.get("deadline"),
        "days_left": None,
        "common_product": False,
        "research_priority_score": 0,
    }


def _write_progress(**kwargs: Any) -> None:
    blob = _load(PROGRESS) or {}
    blob.update(kwargs)
    blob["at"] = _utc()
    blob["build"] = BUILD
    _save(PROGRESS, blob)


def reconciliation_status() -> dict[str, Any]:
    prog = _load(PROGRESS) or {}
    report = _load(REPORT) or {}
    excl = _load(EXCLUSION_IDS) or {}
    return {
        "build": BUILD,
        "progress": prog,
        "report_ready": bool(report.get("total_reconciled")),
        "append_candidates": (
            len(excl.get("append_candidates") or [])
            if excl
            else report.get("append_candidates")
        ),
        "report_summary": {
            k: report.get(k)
            for k in (
                "l23_physical_records",
                "total_reconciled",
                "unexplained_gap",
                "mutually_exclusive_breakdown",
                "frozen_cohort_ids",
                "frozen_ids_found_in_l23",
                "frozen_ids_missing_from_l23",
                "current_production_queue",
                "append_candidates",
                "at",
            )
        }
        if report
        else None,
    }


def reconcile_l23_universe(*, progress_cb: Any | None = None) -> dict[str, Any]:
    """Classify every L23 physical record; reconcile against queue + frozen cohort."""
    _write_progress(status="RUNNING", phase="LOAD_STORE", examined=0)
    store = load_store() or {}
    physical = len(store)
    queue_ids = _queue_ids()
    adv_ids = _adv_deal_ids()
    frozen_ids, frozen_src = _load_frozen_ids()
    frozen_set = set(frozen_ids)
    _write_progress(
        status="RUNNING",
        phase="CLASSIFY",
        examined=0,
        physical=physical,
        queue_size=len(queue_ids),
    )

    by_cat: dict[str, list[str]] = {
        CAT_IN_QUEUE: [],
        CAT_ELIGIBLE_MISSING: [],
        CAT_UNRESOLVED: [],
        CAT_EXPIRED: [],
        CAT_CANCELLED: [],
        CAT_DUPLICATE: [],
        CAT_INELIGIBLE: [],
        CAT_OTHER: [],
    }
    reason_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = {k: [] for k in by_cat}

    for i, (oid, rec) in enumerate(store.items()):
        if not isinstance(rec, dict):
            by_cat[CAT_OTHER].append(str(oid))
            reason_counts["non_dict_record"] += 1
            continue
        oid_s = str(oid).strip()
        try:
            cl = _fast_gate_classify(oid_s, rec)
        except Exception as exc:
            by_cat[CAT_OTHER].append(oid_s)
            reason_counts[f"classify_error:{type(exc).__name__}"] += 1
            continue
        bucket_counts[str(cl.get("visibility_bucket") or "NONE")] += 1
        cat, reason = _category_for_classified(cl, in_queue=oid_s in queue_ids)
        by_cat[cat].append(oid_s)
        reason_counts[f"{cat}:{reason}"] += 1
        if len(samples[cat]) < 15:
            samples[cat].append(
                {
                    "opportunity_id": oid_s,
                    "reason": reason,
                    "visibility_bucket": cl.get("visibility_bucket"),
                    "block_reason": cl.get("block_reason"),
                    "deadline_status": cl.get("deadline_status"),
                    "product_class": cl.get("product_class"),
                    "title": (str(cl.get("product") or ""))[:120],
                }
            )
        if (i + 1) % 2000 == 0:
            _write_progress(
                status="RUNNING",
                phase="CLASSIFY",
                examined=i + 1,
                physical=physical,
            )
            if progress_cb:
                try:
                    progress_cb(examined=i + 1, physical=physical)
                except Exception:
                    pass

    counts = {k: len(v) for k, v in by_cat.items()}
    reconciled = sum(counts.values())

    frozen_in_l23 = frozen_set & set(store.keys())
    frozen_missing = sorted(frozen_set - set(store.keys()))
    queue_in_l23 = queue_ids & set(store.keys())
    queue_not_in_l23 = sorted(queue_ids - set(store.keys()))

    append_candidates = sorted(
        set(by_cat[CAT_ELIGIBLE_MISSING]) | set(by_cat[CAT_UNRESOLVED])
    )

    report = {
        "kind": "L23UniverseReconciliation",
        "build": BUILD,
        "at": _utc(),
        "l23_physical_records": physical,
        "l23_unique_canonical_ids": physical,
        "physical_vs_unique_note": (
            "L23 store is a dict keyed by canonical_opportunity_id; "
            "physical record count equals unique canonical IDs."
        ),
        "frozen_cohort_ids": len(frozen_set),
        "frozen_manifest_source": frozen_src,
        "frozen_ids_found_in_l23": len(frozen_in_l23),
        "frozen_ids_missing_from_l23": len(frozen_missing),
        "current_production_queue": len(queue_ids),
        "existing_queue_ids_found_in_l23": len(queue_in_l23),
        "queue_ids_not_in_l23": len(queue_not_in_l23),
        "adv_deal_ids_including_needs_sample": len(adv_ids),
        "mutually_exclusive_breakdown": counts,
        "total_reconciled": reconciled,
        "unexplained_gap": physical - reconciled,
        "append_candidates": len(append_candidates),
        "adv_visibility_bucket_counts": dict(bucket_counts),
        "top_reason_counts": dict(reason_counts.most_common(40)),
        "samples": samples,
        "sample_frozen_missing": frozen_missing[:25],
        "sample_queue_not_in_l23": queue_not_in_l23[:25],
        "sample_append_candidates": append_candidates[:40],
        "admission_defect": (
            "ADV deals list historically included only WORTH_RESEARCHING + READY_TO_BID; "
            "NEEDS_VERIFICATION (missing/ambiguous deadlines) was truncated to 2000 and "
            "excluded from production queue admission — silent exclusion of unresolved opps."
        ),
    }
    _save(REPORT, report)
    _save(
        EXCLUSION_IDS,
        {
            "kind": "L23UniverseExclusionIds",
            "build": BUILD,
            "at": _utc(),
            "by_category": {k: v for k, v in by_cat.items()},
            "append_candidates": append_candidates,
            "frozen_missing_from_l23": frozen_missing,
            "queue_not_in_l23": queue_not_in_l23,
        },
    )
    _write_progress(
        status="COMPLETE",
        phase="DONE",
        examined=physical,
        physical=physical,
        append_candidates=len(append_candidates),
        total_reconciled=reconciled,
    )
    return report


def start_reconcile_l23_universe_async() -> dict[str, Any]:
    """Start (or report) background reconciliation — avoids HTTP gateway timeouts."""
    global _recon_thread
    with _recon_lock:
        if _recon_thread is not None and _recon_thread.is_alive():
            return {"ok": True, "started": False, "status": "ALREADY_RUNNING", **reconciliation_status()}

        def _run() -> None:
            try:
                reconcile_l23_universe()
            except Exception as exc:
                _write_progress(
                    status="FAILED",
                    phase="ERROR",
                    error=f"{type(exc).__name__}:{exc}"[:240],
                )

        _recon_thread = threading.Thread(
            target=_run, name="l23-universe-reconcile", daemon=True
        )
        _recon_thread.start()
        _write_progress(status="STARTED", phase="THREAD", examined=0)
        return {"ok": True, "started": True, "status": "STARTED", "build": BUILD}


_append_thread: threading.Thread | None = None
APPEND_PROGRESS = f"{OUT}/m3_l23_universe_append_progress_v1.json"


def start_append_missing_async(
    *,
    job_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Background append of reconciled candidates (large payloads exceed HTTP timeouts)."""
    global _append_thread
    with _recon_lock:
        if _append_thread is not None and _append_thread.is_alive():
            return {
                "ok": True,
                "started": False,
                "status": "ALREADY_RUNNING",
                "progress": _load(APPEND_PROGRESS),
            }

        def _run() -> None:
            try:
                _save(
                    APPEND_PROGRESS,
                    {"status": "RUNNING", "at": _utc(), "job_id": job_id, "dry_run": dry_run},
                )
                man = append_missing_to_production_queue(
                    job_id=job_id, dry_run=dry_run, force_reconcile=False
                )
                _save(
                    APPEND_PROGRESS,
                    {"status": "COMPLETE", "at": _utc(), "result": man},
                )
            except Exception as exc:
                _save(
                    APPEND_PROGRESS,
                    {
                        "status": "FAILED",
                        "at": _utc(),
                        "error": f"{type(exc).__name__}:{exc}"[:240],
                    },
                )

        _append_thread = threading.Thread(
            target=_run, name="l23-universe-append", daemon=True
        )
        _append_thread.start()
        return {"ok": True, "started": True, "status": "STARTED", "build": BUILD}


def append_status() -> dict[str, Any]:
    return {"build": BUILD, "progress": _load(APPEND_PROGRESS) or {}}


def append_missing_to_production_queue(
    *,
    job_id: str | None = None,
    dry_run: bool = False,
    max_append: int | None = None,
    force_reconcile: bool = False,
) -> dict[str, Any]:
    """Atomically append missing eligible/unresolved IDs to the end of the live queue.

    Does NOT reset RESUME_CURSOR. Existing first-pass positions stay ahead.
    """
    from buyer_intelligence.full_scale_production_funnel_v1 import (
        BUILD as FSPF_BUILD,
        QUEUE_FILE as FSPF_QUEUE,
        _queue_row_from_any,
        _save_queue_compact,
        _utc as fspf_utc,
    )
    from m3_jobs.store import JobStore

    excl = _load(EXCLUSION_IDS) or {}
    recon = _load(REPORT) or {}
    if force_reconcile or not isinstance(excl.get("by_category"), dict):
        recon = reconcile_l23_universe()
        excl = _load(EXCLUSION_IDS) or {}
    candidates = list(excl.get("append_candidates") or [])
    if max_append is not None:
        candidates = candidates[: int(max_append)]

    store = load_store() or {}
    queue = _load(FSPF_QUEUE) or {}
    rows = list(queue.get("rows") or [])
    existing = {
        str(r.get("opportunity_id") or "").strip()
        for r in rows
        if isinstance(r, dict) and r.get("opportunity_id")
    }
    cursor_before = None
    job_meta = {}
    if job_id:
        try:
            job = JobStore("full_scale_production_funnel_v1").load_job(job_id) or {}
            cursor_before = int(job.get("RESUME_CURSOR") or 0)
            job_meta = {
                "status": job.get("STATUS"),
                "lease_owner": job.get("LEASE_OWNER") or job.get("WORKER_ID"),
                "heartbeat_at": job.get("HEARTBEAT_AT") or job.get("LAST_HEARTBEAT_AT"),
            }
        except Exception as exc:
            job_meta = {"job_load_error": type(exc).__name__}

    # Skip IDs that already have a preserved terminal checkpoint on this job.
    # Bounded: only probe when candidate set is small; large appends rely on
    # queue de-dupe (existing IDs) which already covers active first-pass work.
    skip_completed: set[str] = set()
    if job_id and len(candidates) <= 500:
        try:
            jstore = JobStore("full_scale_production_funnel_v1")
            preserve = {
                "SUCCESS",
                "EXPIRED_EXIT",
                "INELIGIBLE_EXIT",
                "OWNER_ACTION_REQUIRED",
                "SKIPPED_ALREADY_COMPLETE",
                "PARTIAL",
            }
            for oid in candidates:
                prev = jstore.load_item(job_id, oid)
                if prev and str(prev.get("RESULT") or "") in preserve:
                    skip_completed.add(oid)
        except Exception:
            pass

    appended: list[dict[str, Any]] = []
    skipped_dup = 0
    skipped_completed = 0
    skipped_no_row = 0
    admission_reasons: Counter[str] = Counter()
    # Prefer category labels from the reconciliation artifact (no re-classify).
    by_cat_ids = excl.get("by_category") or {}
    unresolved_set = set(by_cat_ids.get(CAT_UNRESOLVED) or [])
    eligible_missing_set = set(by_cat_ids.get(CAT_ELIGIBLE_MISSING) or [])

    for oid in candidates:
        if oid in existing:
            skipped_dup += 1
            continue
        if oid in skip_completed:
            skipped_completed += 1
            continue
        rec = store.get(oid)
        if not isinstance(rec, dict):
            skipped_no_row += 1
            continue
        if oid in eligible_missing_set:
            cat, reason = CAT_ELIGIBLE_MISSING, "from_reconciliation"
        elif oid in unresolved_set:
            cat, reason = CAT_UNRESOLVED, "from_reconciliation"
        else:
            cat, reason = CAT_UNRESOLVED, "append_candidate"
        title = str(rec.get("title") or "")[:240]
        pc = str(rec.get("product_service_classification") or "UNKNOWN_PRODUCTNESS")
        if pc == "TANGIBLE_PRODUCT":
            pc = "PRODUCT"
        elif pc == "MIXED_PRODUCT_SERVICE":
            pc = "MIXED"
        elif pc not in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
            pc = "UNKNOWN_PRODUCTNESS"
        row = {
            "opportunity_id": oid,
            "title": title,
            "buyer": rec.get("buyer") or rec.get("agency") or rec.get("organization"),
            "deadline": rec.get("deadline"),
            "days_left": rec.get("days_left"),
            "authoritative_url": rec.get("authoritative_url") or rec.get("url") or rec.get("source_url"),
            "product_class": pc,
            "funnel_class": "LIVE_ELIGIBLE",
            "deadline_status": str(rec.get("deadline_status") or "DEADLINE_UNKNOWN"),
            "common_product": False,
            "complex_title_needs_qualify": False,
            "selection_score": 0.0,
            "admission_reason": f"{cat}:{reason}",
            "l23_universe_append": True,
        }
        # Soft gate via existing helper when possible (never drop unresolved)
        gated = _queue_row_from_any({**rec, **row}, oid=oid)
        if gated:
            gated["admission_reason"] = row["admission_reason"]
            gated["l23_universe_append"] = True
            row = gated
        admission_reasons[str(row.get("admission_reason"))] += 1
        appended.append(row)

    queue_before = len(rows)
    if not dry_run and appended:
        rows.extend(appended)
        queue["rows"] = rows
        queue["eligible_total"] = len(rows)
        queue["build"] = FSPF_BUILD
        queue["updated_at"] = fspf_utc()
        queue["l23_universe_append_at"] = fspf_utc()
        queue["l23_universe_append_count"] = int(queue.get("l23_universe_append_count") or 0) + len(
            appended
        )
        queue["NOTE"] = (
            "Appended missing L23 eligible/unresolved after existing first-pass rows; "
            "RESUME_CURSOR unchanged."
        )
        _save_queue_compact(queue)

    cursor_after = cursor_before
    if job_id and not dry_run:
        try:
            jstore = JobStore("full_scale_production_funnel_v1")
            job = jstore.load_job(job_id) or {}
            cursor_after = int(job.get("RESUME_CURSOR") or 0)
            meta = dict(job.get("META") or {})
            meta["l23_universe_append_at"] = fspf_utc()
            meta["l23_universe_append_count"] = len(appended)
            meta["eligible_total_after_append"] = queue_before + len(appended)
            job["META"] = meta
            # Reflect new queue size for scorecards — do NOT touch RESUME_CURSOR.
            job["TOTAL"] = queue_before + len(appended)
            jstore.save_job(job)
        except Exception as exc:
            job_meta["save_error"] = type(exc).__name__

    manifest = {
        "kind": "L23UniverseAppendManifest",
        "build": BUILD,
        "at": _utc(),
        "dry_run": dry_run,
        "job_id": job_id,
        "job_meta": job_meta,
        "resume_cursor_before": cursor_before,
        "resume_cursor_after": cursor_after,
        "cursor_unchanged": cursor_before == cursor_after,
        "queue_before": queue_before,
        "queue_after": queue_before + (0 if dry_run else len(appended)),
        "candidates": len(candidates),
        "appended": len(appended),
        "skipped_already_queued": skipped_dup,
        "skipped_completed_checkpoint": skipped_completed,
        "skipped_no_row": skipped_no_row,
        "admission_reason_counts": dict(admission_reasons),
        "sample_appended_ids": [r["opportunity_id"] for r in appended[:40]],
        "reconciliation_summary": {
            "l23_physical": recon.get("l23_physical_records"),
            "total_reconciled": recon.get("total_reconciled"),
            "breakdown": recon.get("mutually_exclusive_breakdown"),
            "append_candidates": recon.get("append_candidates"),
        },
    }
    _save(APPEND_MANIFEST, manifest)
    return manifest
