"""Full-population solicitation package acquisition jobs (Build 002E).

Persistent, resumable, prioritized — same shared acquire_solicitation_package engine.
Does not permanently exclude lower-priority records.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.models import Opportunity, PackageAcquisitionJob, PackageJobItem, SolicitationPackage
from m3.research.package import (
    acquire_solicitation_package,
    package_to_item_enrichment,
    storage_consumption_report,
)

_worker_lock = threading.Lock()
_active_workers: dict[str, threading.Thread] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def populations_root() -> Path:
    root = get_settings().data_root() / "acceptance_populations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_population_file(run_id: str | UUID, items: list[dict[str, Any]]) -> Path:
    """Persist every unique solicitation for freeze/replay (not truncated)."""
    path = populations_root() / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            if not isinstance(it, dict):
                continue
            # Drop huge nested fields to keep freeze file manageable
            slim = {
                k: it.get(k)
                for k in (
                    "external_key",
                    "title",
                    "display_title",
                    "solicitation_number",
                    "buyer_name",
                    "buyer",
                    "official_url",
                    "source_key",
                    "requirement_class",
                    "product_category",
                    "procurement_type",
                    "solicitation_status",
                    "deadline_at",
                    "geo_state",
                    "quantity",
                    "description",
                    "include_in_product_resale",
                    "uncertain_resolution",
                    "attachment_candidates",
                    "classification_evidence",
                    "record_type",
                    "record_type_evidence",
                    "verified_individual_solicitation",
                    "identity_strength",
                    "source_purpose",
                    "discovery_url",
                )
                if k in it or it.get(k) is not None
            }
            f.write(json.dumps(slim, default=str) + "\n")
    meta = {
        "run_id": str(run_id),
        "count": len(items),
        "written_at": _now().isoformat(),
        "path": str(path),
    }
    (populations_root() / f"{run_id}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def load_population_file(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    items: list[dict[str, Any]] = []
    if not p.exists():
        return items
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def prioritize_population(items: list[dict[str, Any]], *, saved_keys: set[str] | None = None) -> list[dict[str, Any]]:
    """Order only — never drops records."""
    saved_keys = saved_keys or set()
    inactive = {"closed", "closed_by_deadline", "cancelled", "historical_award", "forecast"}

    def score(it: dict) -> tuple:
        ek = str(it.get("external_key") or "")
        req = (it.get("requirement_class") or "").lower()
        status = (it.get("solicitation_status") or "unknown").lower()
        saved = 0 if ek in saved_keys else 1
        product = 0 if req in {
            "product_supply",
            "product_with_incidental_services",
            "product_with_significant_installation",
            "product_rental_leasing",
            "mixed_product_service",
        } else 1
        uncertain = 0 if req in {"unknown_mixed", "unknown", "pending_classification", ""} else 1
        active = 0 if status not in inactive else 2
        # sooner deadline first
        dl = str(it.get("deadline_at") or "9999")
        return (saved, active, product, uncertain, dl, ek)

    return sorted(items, key=score)


def reconcile_population(
    current: list[dict[str, Any]],
    *,
    baseline_count: int = 9799,
) -> dict[str, Any]:
    """Account for every record disposition vs baseline size (not silent drops)."""
    inactive = {"closed", "closed_by_deadline", "cancelled", "historical_award"}
    forecast = {"forecast"}
    by_status: dict[str, int] = {}
    by_class: dict[str, int] = {}
    active = 0
    for it in current:
        st = (it.get("solicitation_status") or "unknown").lower()
        by_status[st] = by_status.get(st, 0) + 1
        cls = (it.get("requirement_class") or "unknown_mixed").lower()
        by_class[cls] = by_class.get(cls, 0) + 1
        if st not in inactive and st not in forecast:
            active += 1
    return {
        "baseline_002c_count": baseline_count,
        "frozen_unique_count": len(current),
        "delta_vs_baseline": len(current) - baseline_count,
        "verified_active_approx": active,
        "by_status": by_status,
        "by_class": by_class,
        "note": "Delta reflects source/page coverage and publication churn — not silent exclusion",
    }


def _slim_item(it: dict[str, Any]) -> dict[str, Any]:
    return {
        k: it.get(k)
        for k in (
            "external_key",
            "title",
            "display_title",
            "solicitation_number",
            "buyer_name",
            "buyer",
            "official_url",
            "source_key",
            "requirement_class",
            "product_category",
            "procurement_type",
            "solicitation_status",
            "deadline_at",
            "geo_state",
            "quantity",
            "description",
            "include_in_product_resale",
            "uncertain_resolution",
            "attachment_candidates",
            "classification_evidence",
            "record_type",
            "record_type_evidence",
            "verified_individual_solicitation",
            "identity_strength",
            "source_purpose",
            "discovery_url",
        )
        if it.get(k) is not None
    }


def create_population_job(
    db: Session,
    items: list[dict[str, Any]],
    *,
    run_id: str | UUID | None = None,
    mode: str = "population",
    created_by=None,
    population_path: str | None = None,
) -> PackageAcquisitionJob:
    saved_keys = {r[0] for r in db.query(Opportunity.external_key).all() if r[0]}
    ordered = prioritize_population(items, saved_keys=saved_keys)
    rid = run_id or f"freeze_{int(time.time())}"
    if not population_path:
        population_path = str(write_population_file(rid, ordered))
    else:
        write_population_file(Path(population_path).stem, ordered)
        population_path = str(populations_root() / f"{Path(population_path).stem}.jsonl")

    job = PackageAcquisitionJob(
        status="queued",
        priority="normal",
        mode=mode,
        total=len(ordered),
        queued=len(ordered),
        processing=0,
        completed=0,
        partial=0,
        restricted=0,
        failed=0,
        created_by=created_by,
        progress={
            "identified_docs": 0,
            "downloaded_docs": 0,
            "extracted_docs": 0,
            "missing_docs": 0,
            "newly_product": 0,
            "newly_resolved": 0,
            "by_status": {},
            "by_source": {},
            "by_platform": {},
            "verification_required": 0,
            "update_available": 0,
            "not_acquired": 0,
            "inventory_verified": 0,
            "detail_resolved": 0,
        },
        checkpoint={
            "cursor": 0,
            "population_path": population_path,
            "research_run_id": str(run_id) if run_id else None,
            "batch_size": 40,
            "errors_sample": [],
            "queue": "postgres_package_job_items",
        },
    )
    db.add(job)
    db.flush()
    # Durable queue in Postgres (Railway + local share DATABASE_URL)
    chunk: list[PackageJobItem] = []
    for seq, it in enumerate(ordered):
        chunk.append(
            PackageJobItem(
                job_id=job.id,
                seq=seq,
                external_key=str(it.get("external_key") or "")[:255] or None,
                payload=_slim_item(it),
                status="pending",
            )
        )
        if len(chunk) >= 500:
            db.add_all(chunk)
            db.flush()
            chunk = []
    if chunk:
        db.add_all(chunk)
    db.commit()
    db.refresh(job)
    return job


def replan_pending_progressive(
    db: Session,
    job_id: UUID,
    *,
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark pending confirmed non-product / inactive as skipped; keep uncertain + products.

    Preserves completed work. Does not delete ambiguous opportunities.
    """
    from m3.research.qualification import default_auto_criteria, qualify_for_acquisition

    criteria = criteria or default_auto_criteria()
    job = db.get(PackageAcquisitionJob, job_id)
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    saved_keys = {r[0] for r in db.query(Opportunity.external_key).all() if r[0]}
    pending = (
        db.query(PackageJobItem)
        .filter(PackageJobItem.job_id == job_id, PackageJobItem.status == "pending")
        .all()
    )
    counts = {
        "examined": 0,
        "skipped_non_product": 0,
        "skipped_inactive": 0,
        "skipped_non_solicitation_identity": 0,
        "kept_eligible": 0,
        "kept_targeted": 0,
        "kept_saved": 0,
    }
    for row in pending:
        counts["examined"] += 1
        it = dict(row.payload or {})
        # Apply identity if missing (safe reprocess of frozen payloads)
        if not it.get("record_type"):
            from m3.research.identity import apply_identity

            it = apply_identity(it)
            row.payload = {**(row.payload or {}), **{k: it.get(k) for k in (
                "record_type",
                "record_type_evidence",
                "verified_individual_solicitation",
                "identity_strength",
            ) if it.get(k) is not None}}
        ek = str(it.get("external_key") or row.external_key or "")
        q = qualify_for_acquisition(it, criteria=criteria, saved=ek in saved_keys)
        if q["disposition"] in {
            "SKIP_CONFIRMED_NON_PRODUCT",
            "SKIP_INACTIVE_STATUS",
            "SKIP_NON_SOLICITATION_IDENTITY",
        }:
            row.status = "skipped"
            row.package_status = "NOT_ACQUIRED"
            row.error = q["reason"][:200]
            row.processed_at = _now()
            payload = dict(row.payload or {})
            payload["_qualification"] = q
            payload["_skip_reason"] = q["reason"]
            row.payload = payload
            db.add(row)
            if q["disposition"] == "SKIP_CONFIRMED_NON_PRODUCT":
                counts["skipped_non_product"] += 1
            elif q["disposition"] == "SKIP_NON_SOLICITATION_IDENTITY":
                counts["skipped_non_solicitation_identity"] += 1
            else:
                counts["skipped_inactive"] += 1
        elif q["disposition"] == "SAVED_PRIORITY":
            counts["kept_saved"] += 1
        elif q["acquire_full_package"]:
            counts["kept_eligible"] += 1
        else:
            counts["kept_targeted"] += 1
    prog = dict(job.progress or {})
    prog["replan_002g"] = counts
    prog["unnecessary_downloads_avoided_pending"] = (
        counts["skipped_non_product"]
        + counts["skipped_inactive"]
        + counts["skipped_non_solicitation_identity"]
    )
    job.progress = prog
    job.status = "paused"
    ck = dict(job.checkpoint or {})
    ck["progressive_qualification"] = True
    ck["replan_at"] = _now().isoformat()
    job.checkpoint = ck
    job.queued = (
        db.query(PackageJobItem)
        .filter(PackageJobItem.job_id == job_id, PackageJobItem.status == "pending")
        .count()
    )
    db.add(job)
    db.commit()
    return {"ok": True, "job_id": str(job_id), **counts, "pending_remaining": job.queued}


def process_job_batch(db: Session, job_id: UUID, *, batch_size: int | None = None) -> dict[str, Any]:
    """Process next N pending queue items. Safe to call repeatedly; resumable after restart."""
    job = db.get(PackageAcquisitionJob, job_id)
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    if job.status in {"cancelled", "completed", "paused"}:
        return {"ok": True, "status": job.status, "done": job.status == "completed"}

    if job.status == "queued":
        job.status = "running"
        job.started_at = job.started_at or _now()
    elif job.status == "interrupted":
        job.status = "running"

    ck = dict(job.checkpoint or {})
    prog = dict(job.progress or {})
    bs = int(batch_size or ck.get("batch_size") or 40)
    research_run_id = ck.get("research_run_id")
    run_uuid = None
    if research_run_id:
        try:
            run_uuid = UUID(str(research_run_id))
        except Exception:
            run_uuid = None

    rows = (
        db.query(PackageJobItem)
        .filter(PackageJobItem.job_id == job_id, PackageJobItem.status == "pending")
        .order_by(PackageJobItem.seq.asc())
        .limit(bs)
        .all()
    )
    job.processing = len(rows)
    pending_left = (
        db.query(PackageJobItem)
        .filter(PackageJobItem.job_id == job_id, PackageJobItem.status == "pending")
        .count()
    )
    job.queued = max(0, pending_left - len(rows))
    db.add(job)
    db.commit()

    errors_sample = list(ck.get("errors_sample") or [])

    from m3.research.classify import PRODUCT_CLASSES
    from m3.research.perf import timed
    from m3.research.qualification import default_auto_criteria, qualify_for_acquisition

    criteria = (ck.get("qualification_criteria") or default_auto_criteria())
    progressive = bool(ck.get("progressive_qualification"))
    prog.setdefault("skipped_full_package", 0)
    prog.setdefault("targeted_investigations", 0)
    prog.setdefault("full_packages_attempted", 0)
    prog.setdefault("unnecessary_downloads_avoided", int(prog.get("unnecessary_downloads_avoided") or 0))

    for row in rows:
        it = dict(row.payload or {})
        before = (it.get("requirement_class") or "").lower()
        ek = str(it.get("external_key") or row.external_key or "")
        try:
            with timed("db_lookup_saved"):
                saved = db.query(Opportunity).filter_by(external_key=ek).one_or_none() if ek else None
            q = qualify_for_acquisition(
                it,
                criteria=criteria,
                saved=bool(saved),
                manual_override=bool(it.get("manual_override")),
            )
            it["_qualification"] = q
            mode = "full"
            if progressive or ck.get("progressive_qualification"):
                if not q["acquire_full_package"] and not q.get("targeted_only"):
                    it["_skip_reason"] = q["reason"]
                    mode = "skip_mark"
                    prog["skipped_full_package"] = int(prog.get("skipped_full_package") or 0) + 1
                    prog["unnecessary_downloads_avoided"] = (
                        int(prog.get("unnecessary_downloads_avoided") or 0) + 1
                    )
                elif q.get("targeted_only"):
                    mode = "targeted"
                    prog["targeted_investigations"] = int(prog.get("targeted_investigations") or 0) + 1
                else:
                    prog["full_packages_attempted"] = int(prog.get("full_packages_attempted") or 0) + 1
            else:
                prog["full_packages_attempted"] = int(prog.get("full_packages_attempted") or 0) + 1

            pkg = acquire_solicitation_package(
                db,
                it,
                opportunity_id=saved.id if saved else None,
                research_run_id=run_uuid,
                max_documents=200 if mode == "full" else 1,
                mode=mode,
            )
            enrich = package_to_item_enrichment(pkg)
            after = (enrich.get("requirement_class") or before).lower()

            # Escalate targeted → full when classification recovers to product
            if mode == "targeted" and after in PRODUCT_CLASSES:
                prog["newly_product"] = int(prog.get("newly_product") or 0) + 1
                prog["newly_resolved"] = int(prog.get("newly_resolved") or 0) + 1
                pkg = acquire_solicitation_package(
                    db,
                    {**it, **enrich},
                    opportunity_id=saved.id if saved else None,
                    research_run_id=run_uuid,
                    max_documents=200,
                    mode="full",
                    refresh=True,
                )
                enrich = package_to_item_enrichment(pkg)
                after = (enrich.get("requirement_class") or after).lower()
                prog["full_packages_attempted"] = int(prog.get("full_packages_attempted") or 0) + 1
            elif before in {"unknown_mixed", "unknown", "pending_classification", ""} and after not in {
                "unknown_mixed",
                "unknown",
                "pending_classification",
                "",
            }:
                prog["newly_resolved"] = int(prog.get("newly_resolved") or 0) + 1
                if after in PRODUCT_CLASSES and before not in PRODUCT_CLASSES:
                    prog["newly_product"] = int(prog.get("newly_product") or 0) + 1

            st = pkg.package_status if mode != "skip_mark" else "SKIPPED_FULL"
            row.package_status = st if mode != "skip_mark" else "NOT_ACQUIRED"
            row.status = "done" if mode != "skip_mark" else "skipped"
            row.processed_at = _now()
            by_st = dict(prog.get("by_status") or {})
            by_st[st] = int(by_st.get(st) or 0) + 1
            prog["by_status"] = by_st
            by_disp = dict(prog.get("by_disposition") or {})
            by_disp[q["disposition"]] = int(by_disp.get(q["disposition"]) or 0) + 1
            prog["by_disposition"] = by_disp
            if mode != "skip_mark":
                if pkg.package_status == "COMPLETE":
                    job.completed = int(job.completed or 0) + 1
                elif pkg.package_status == "PARTIAL":
                    job.partial = int(job.partial or 0) + 1
                elif pkg.package_status == "ACCESS_RESTRICTED":
                    job.restricted = int(job.restricted or 0) + 1
                elif pkg.package_status == "FAILED":
                    job.failed = int(job.failed or 0) + 1
                elif pkg.package_status == "VERIFICATION_REQUIRED":
                    prog["verification_required"] = int(prog.get("verification_required") or 0) + 1
                elif pkg.package_status == "UPDATE_AVAILABLE":
                    prog["update_available"] = int(prog.get("update_available") or 0) + 1

                prog["identified_docs"] = int(prog.get("identified_docs") or 0) + int(pkg.identified_count or 0)
                prog["downloaded_docs"] = int(prog.get("downloaded_docs") or 0) + int(pkg.downloaded_count or 0)
                prog["extracted_docs"] = int(prog.get("extracted_docs") or 0) + int(pkg.extracted_count or 0)
                prog["missing_docs"] = int(prog.get("missing_docs") or 0) + int(pkg.missing_count or 0)
                if (pkg.detail_fields or {}).get("attachment_inventory_examined"):
                    prog["inventory_verified"] = int(prog.get("inventory_verified") or 0) + 1
                if pkg.url_kind in {"detail", "document"} and pkg.authoritative_url:
                    prog["detail_resolved"] = int(prog.get("detail_resolved") or 0) + 1

                sk = pkg.source_key or "unknown"
                pl = pkg.platform or "unknown"
                for bucket, key in (("by_source", sk), ("by_platform", pl)):
                    b = dict(prog.get(bucket) or {})
                    entry = dict(b.get(key) or {"n": 0, "downloaded": 0, "complete": 0, "failed": 0})
                    entry["n"] += 1
                    entry["downloaded"] += int(pkg.downloaded_count or 0)
                    if pkg.package_status == "COMPLETE":
                        entry["complete"] += 1
                    if pkg.package_status == "FAILED":
                        entry["failed"] += 1
                    b[key] = entry
                    prog[bucket] = b
            db.add(row)
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            # Re-bind job after rollback
            job = db.get(PackageAcquisitionJob, job_id) or job
            row = db.get(PackageJobItem, row.id) or row
            row.status = "error"
            row.error = type(exc).__name__[:200]
            row.processed_at = _now()
            db.add(row)
            try:
                job.failed = int(job.failed or 0) + 1
                by_st = dict(prog.get("by_status") or {})
                by_st["FAILED"] = int(by_st.get("FAILED") or 0) + 1
                prog["by_status"] = by_st
                db.add(job)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            if len(errors_sample) < 40:
                errors_sample.append({"external_key": ek, "error": type(exc).__name__})

    done_count = (
        db.query(PackageJobItem)
        .filter(
            PackageJobItem.job_id == job_id,
            PackageJobItem.status.in_(["done", "error", "skipped"]),
        )
        .count()
    )
    pending = (
        db.query(PackageJobItem)
        .filter(PackageJobItem.job_id == job_id, PackageJobItem.status == "pending")
        .count()
    )
    ck["cursor"] = done_count
    ck["errors_sample"] = errors_sample
    ck["last_batch_at"] = _now().isoformat()
    job.checkpoint = ck
    job.progress = prog
    job.processing = 0
    job.queued = pending
    done = pending == 0
    if done:
        job.status = "completed"
        job.finished_at = _now()
    else:
        job.status = "running"
    db.add(job)
    db.commit()
    return {
        "ok": True,
        "job_id": str(job.id),
        "status": job.status,
        "cursor": done_count,
        "total": int(job.total or 0),
        "done": done,
        "progress": prog,
        "completed": job.completed,
        "partial": job.partial,
        "restricted": job.restricted,
        "failed": job.failed,
        "pending": pending,
    }


def run_job_until_idle(
    db_factory,
    job_id: UUID,
    *,
    batch_size: int = 40,
    max_batches: int | None = None,
    pause_seconds: float = 0.05,
) -> dict[str, Any]:
    """Drain a job using short DB sessions per batch (survives as successive calls)."""
    batches = 0
    last: dict[str, Any] = {}
    while True:
        if max_batches is not None and batches >= max_batches:
            last["stopped_reason"] = "max_batches"
            break
        db = db_factory()
        try:
            job = db.get(PackageAcquisitionJob, job_id)
            if job and job.status == "paused":
                last = {"ok": True, "status": "paused", "done": False, "stopped_reason": "paused"}
                break
            last = process_job_batch(db, job_id, batch_size=batch_size)
        finally:
            db.close()
        batches += 1
        if last.get("done") or not last.get("ok") or last.get("status") == "paused":
            break
        if pause_seconds:
            time.sleep(pause_seconds)
    last["batches_run"] = batches
    return last


def unpause_job(db: Session, job_id: UUID) -> PackageAcquisitionJob | None:
    job = db.get(PackageAcquisitionJob, job_id)
    if not job:
        return None
    if job.status == "paused":
        job.status = "queued"
        ck = dict(job.checkpoint or {})
        ck["unpaused_at"] = _now().isoformat()
        job.checkpoint = ck
        db.add(job)
        db.commit()
        db.refresh(job)
    return job


def start_background_worker(job_id: UUID, *, batch_size: int = 40, workers: int = 1) -> bool:
    """Start daemon thread(s) that process batches until complete or interrupted.

    workers>1 runs controlled parallel batch drainers (each takes next pending rows).
    Per-source politeness is enforced inside HTTP client timeouts; keep workers modest.
    """
    key = str(job_id)
    with _worker_lock:
        t = _active_workers.get(key)
        if t and t.is_alive():
            return False

        def _loop():
            from m3.db import SessionLocal

            try:
                # Parallelism via multiple sequential batch loops is safer with DB;
                # single loop with larger batches + connection reuse is default.
                run_job_until_idle(SessionLocal, job_id, batch_size=batch_size, max_batches=None)
            finally:
                with _worker_lock:
                    _active_workers.pop(key, None)

        th = threading.Thread(target=_loop, name=f"pkg-acq-{key[:8]}", daemon=True)
        _active_workers[key] = th
        th.start()
        # Optional companion workers for independent source throughput
        for i in range(1, max(1, min(workers, 4))):
            def _loop_i(n=i):
                from m3.db import SessionLocal

                run_job_until_idle(SessionLocal, job_id, batch_size=max(5, batch_size // 2), max_batches=None)

            threading.Thread(target=_loop_i, name=f"pkg-acq-{key[:8]}-w{i}", daemon=True).start()
        return True


def resume_incomplete_jobs(db: Session) -> list[str]:
    """On startup: mark interrupted runners and kick workers. Never auto-resume paused."""
    resumed: list[str] = []
    rows = (
        db.query(PackageAcquisitionJob)
        .filter(PackageAcquisitionJob.status.in_(["running", "queued", "interrupted"]))
        .order_by(PackageAcquisitionJob.created_at.asc())
        .limit(5)
        .all()
    )
    for job in rows:
        if job.status == "running":
            job.status = "interrupted"
            db.add(job)
            db.commit()
        if start_background_worker(job.id):
            resumed.append(str(job.id))
    return resumed


def job_metrics(db: Session, job: PackageAcquisitionJob) -> dict[str, Any]:
    storage = storage_consumption_report(db)
    ck = job.checkpoint or {}
    prog = job.progress or {}
    total = int(job.total or 0)
    cursor = int(ck.get("cursor") or 0)
    attempted = cursor
    return {
        "job_id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "total": total,
        "cursor": cursor,
        "acquisition_attempted": attempted,
        "acquisition_outstanding": max(0, total - attempted),
        "coverage": (attempted / total) if total else None,
        "completed": job.completed,
        "partial": job.partial,
        "restricted": job.restricted,
        "failed": job.failed,
        "verification_required": prog.get("verification_required"),
        "progress": prog,
        "checkpoint": {
            "population_path": ck.get("population_path"),
            "last_batch_at": ck.get("last_batch_at"),
            "errors_sample": ck.get("errors_sample"),
        },
        "storage": storage,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def recount_packages(db: Session) -> dict[str, Any]:
    pkgs = db.query(SolicitationPackage).all()
    by_status: dict[str, int] = {}
    identified = downloaded = missing = extracted = 0
    detail_resolved = inventory = 0
    for p in pkgs:
        by_status[p.package_status] = by_status.get(p.package_status, 0) + 1
        identified += int(p.identified_count or 0)
        downloaded += int(p.downloaded_count or 0)
        missing += int(p.missing_count or 0)
        extracted += int(p.extracted_count or 0)
        if p.url_kind in {"detail", "document"} and p.authoritative_url:
            detail_resolved += 1
        if (p.detail_fields or {}).get("attachment_inventory_examined"):
            inventory += 1
    return {
        "packages": len(pkgs),
        "by_status": by_status,
        "identified_documents": identified,
        "downloaded_documents": downloaded,
        "missing_documents": missing,
        "extracted_documents": extracted,
        "detail_urls_resolved": detail_resolved,
        "inventories_verified": inventory,
        "document_download_success_rate": (downloaded / identified) if identified else None,
        "extraction_success_rate": (extracted / downloaded) if downloaded else None,
        "package_complete_rate": (by_status.get("COMPLETE", 0) / len(pkgs)) if pkgs else None,
    }
