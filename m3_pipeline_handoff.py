"""Durable, resumable discovery → pipeline handoff with reconciliation.

Discovery must not finish while downstream state is incomplete.
Railway restarts must not lose survivors already checkpointed.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from application_clock import now_utc
from solicitation_identity import identity_key

log = logging.getLogger("govtracker.m3_pipeline_handoff")

HANDOFF_SETTINGS_KEY = "m3_discovery_handoff_checkpoint_v1"
DEFAULT_CHECKPOINT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_handoff_checkpoint.json"
CHECKPOINT_EVERY = 25
CHECKPOINT_EVERY_LARGE = 500  # large national/federal batches
LARGE_BATCH_THRESHOLD = 500
MAX_HANDOFF_RETRIES = 2
SURVIVORS_BLOB_KEY = "m3_discovery_handoff_survivors_v1"


def _utc() -> str:
    return now_utc().isoformat()


def _read_checkpoint_db() -> dict[str, Any] | None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == HANDOFF_SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return None
            data = json.loads(row.value)
            return data if isinstance(data, dict) else None
        finally:
            db.close()
    except Exception:
        log.exception("Failed reading handoff checkpoint from AppSetting")
        return None


def _write_checkpoint_db(payload: dict[str, Any]) -> None:
    try:
        from database import SessionLocal
        from models import AppSetting

        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == HANDOFF_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=HANDOFF_SETTINGS_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Failed writing handoff checkpoint to AppSetting")


def _write_survivors_blob(run_id: str, survivors: list[dict[str, Any]]) -> None:
    """Persist full survivors once (separate from progress cursor checkpoint)."""
    payload = {
        "kind": "M3HandoffSurvivorsBlob",
        "run_id": run_id,
        "updated_at": _utc(),
        "count": len(survivors),
        "survivors": survivors,
    }
    path = DEFAULT_CHECKPOINT_PATH.parent / f"m3_handoff_survivors_{run_id}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed writing survivors blob file")
    try:
        from database import SessionLocal
        from models import AppSetting

        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SURVIVORS_BLOB_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=SURVIVORS_BLOB_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Failed writing survivors blob to AppSetting")


def _read_survivors_blob(run_id: str | None = None) -> list[dict[str, Any]] | None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SURVIVORS_BLOB_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict) and (not run_id or data.get("run_id") == run_id):
                    surv = data.get("survivors")
                    return surv if isinstance(surv, list) else None
        finally:
            db.close()
    except Exception:
        log.exception("Failed reading survivors blob")
    return None


def save_handoff_checkpoint(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Save progress checkpoint — survivors stored separately for large batches."""
    payload = deepcopy(payload)
    payload["updated_at"] = _utc()
    # Strip bulky survivors from frequent progress writes when blob flag set
    slim = payload
    if payload.get("survivors_externalized") and "survivors" in payload:
        slim = {k: v for k, v in payload.items() if k != "survivors"}
        slim["survivors_count"] = payload.get("discovery_count") or payload.get("survivors_count")
    path = path or DEFAULT_CHECKPOINT_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed writing handoff checkpoint file")
    _write_checkpoint_db(slim)
    return payload


def begin_handoff_checkpoint(
    *,
    run_id: str,
    survivors: list[dict[str, Any]],
    discovery_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist discovery survivors before any pipeline mutation."""
    keys = []
    compact: list[dict[str, Any]] = []
    for r in survivors:
        if not isinstance(r, dict):
            continue
        cid = identity_key(r)
        keys.append(cid)
        compact.append(r)
    large = len(compact) >= LARGE_BATCH_THRESHOLD
    if large:
        _write_survivors_blob(run_id, compact)
    payload = {
        "kind": "M3HandoffCheckpoint",
        "status": "PENDING",
        "run_id": run_id,
        "started_at": _utc(),
        "discovery_count": len(compact),
        "survivor_keys": keys,
        "survivors": compact if not large else [],
        "survivors_externalized": large,
        "processed_keys": [],
        "failed_keys": [],
        "failed_upserts": [],
        "pipeline_new": 0,
        "pipeline_updated": 0,
        "research_queued": 0,
        "cursor": 0,
        "retries": 0,
        "reconciliation": None,
        "discovery_metrics": {
            k: discovery_metrics.get(k)
            for k in (
                "sources_attempted",
                "sources_successful",
                "sources_failed",
                "raw_records",
                "unique_records",
                "listing_records",
            )
            if isinstance(discovery_metrics, dict)
        }
        if discovery_metrics
        else {},
    }
    return save_handoff_checkpoint(payload)


def load_handoff_checkpoint(path: Path | None = None) -> dict[str, Any] | None:
    db_data = _read_checkpoint_db()
    if db_data:
        return db_data
    path = path or DEFAULT_CHECKPOINT_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def clear_handoff_checkpoint(path: Path | None = None) -> None:
    empty = {"kind": "M3HandoffCheckpoint", "status": "CLEARED", "updated_at": _utc()}
    save_handoff_checkpoint(empty, path=path)
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SURVIVORS_BLOB_KEY).one_or_none()
            if row:
                row.value = json.dumps({"kind": "M3HandoffSurvivorsBlob", "status": "CLEARED", "updated_at": _utc()})
                db.commit()
        finally:
            db.close()
    except Exception:
        pass


def reconcile_handoff(
    *,
    survivors: list[dict[str, Any]],
    store: Any,
) -> dict[str, Any]:
    """Verify every survivor identity exists in the pipeline store."""
    # Prefer O(1) key set without deepcopying all rows
    pipeline_ids: set[str] = set()
    try:
        rows_map = getattr(store, "_rows", None)
        if isinstance(rows_map, dict):
            pipeline_ids = {str(k) for k in rows_map.keys()}
        else:
            pipeline_ids = {r.get("canonical_id") for r in store.all() if r.get("canonical_id")}
    except Exception:
        pipeline_ids = {r.get("canonical_id") for r in store.all() if r.get("canonical_id")}
    discovered = 0
    present = 0
    missing: list[str] = []
    duplicate_handoffs = 0
    seen_keys: set[str] = set()
    for r in survivors:
        if not isinstance(r, dict):
            continue
        discovered += 1
        cid = identity_key(r)
        if cid in seen_keys:
            duplicate_handoffs += 1
        seen_keys.add(cid)
        if cid in pipeline_ids:
            present += 1
        else:
            missing.append(cid)
    return {
        "kind": "M3HandoffReconciliation",
        "checked_at": _utc(),
        "DISCOVERY_COUNT": discovered,
        "PIPELINE_COUNT": present,
        "PIPELINE_STORE_TOTAL": len(pipeline_ids),
        "MISSING_FROM_PIPELINE": len(missing),
        "missing_keys": missing[:200],
        "DUPLICATE_HANDOFFS": duplicate_handoffs,
        "FAILED_UPSERTS": 0,
        "match": len(missing) == 0 and discovered == present,
    }


def run_durable_handoff(
    *,
    run_id: str,
    survivors: list[dict[str, Any]],
    store: Any,
    orch: Any,
    discovery_metrics: dict[str, Any] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    checkpoint_every: int = CHECKPOINT_EVERY,
    resume: bool = True,
) -> dict[str, Any]:
    """
    Idempotent upsert of all survivors with checkpointed progress.

    advance is deferred — research_queued is set by ingest; research worker drains.
    Individual record failures do not abort the batch.
    """
    # Auto-scale checkpoint cadence for large national/federal batches
    if checkpoint_every == CHECKPOINT_EVERY and len(survivors) >= LARGE_BATCH_THRESHOLD:
        checkpoint_every = CHECKPOINT_EVERY_LARGE
    if len(survivors) >= 2000:
        checkpoint_every = max(checkpoint_every, 1000)

    ckpt = load_handoff_checkpoint() if resume else None
    if (
        resume
        and ckpt
        and ckpt.get("run_id") == run_id
        and ckpt.get("status") in {"PENDING", "IN_PROGRESS", "RETRYING"}
        and (
            (isinstance(ckpt.get("survivors"), list) and ckpt.get("survivors"))
            or ckpt.get("survivors_externalized")
        )
    ):
        if ckpt.get("survivors_externalized") and not ckpt.get("survivors"):
            blob = _read_survivors_blob(run_id)
            work = list(blob or survivors)
        else:
            work = list(ckpt.get("survivors") or survivors)
        processed = set(ckpt.get("processed_keys") or [])
        failed_keys = list(ckpt.get("failed_keys") or [])
        failed_upserts = list(ckpt.get("failed_upserts") or [])
        pipeline_new = int(ckpt.get("pipeline_new") or 0)
        pipeline_updated = int(ckpt.get("pipeline_updated") or 0)
        research_queued = int(ckpt.get("research_queued") or 0)
        cursor = int(ckpt.get("cursor") or 0)
        retries = int(ckpt.get("retries") or 0)
    else:
        ckpt = begin_handoff_checkpoint(
            run_id=run_id,
            survivors=survivors,
            discovery_metrics=discovery_metrics,
        )
        if ckpt.get("survivors_externalized"):
            work = list(survivors)
        else:
            work = list(ckpt["survivors"])
        processed = set()
        failed_keys = []
        failed_upserts = []
        pipeline_new = 0
        pipeline_updated = 0
        research_queued = 0
        cursor = 0
        retries = 0

    ckpt["status"] = "IN_PROGRESS"
    # Keep survivors out of frequent progress checkpoints
    if ckpt.get("survivors_externalized"):
        ckpt["survivors"] = []
    save_handoff_checkpoint(ckpt)

    results: list[dict[str, Any]] = []
    since_save = 0
    t0 = now_utc()

    def _flush_progress(*, force: bool = False) -> None:
        nonlocal since_save
        if not force and since_save < checkpoint_every:
            return
        try:
            # Mid-batch: file cache only (no durable DB merge/read). Final flush is durable.
            store.save(durable_write=force)
        except TypeError:
            store.save()
        except Exception:
            log.exception("Handoff checkpoint store.save failed")
        ckpt.update(
            {
                "status": "IN_PROGRESS",
                "cursor": cursor,
                "processed_keys": list(processed)[-20000:] if len(processed) > 20000 else list(processed),
                "failed_keys": failed_keys[-500:],
                "failed_upserts": failed_upserts[-100:],
                "pipeline_new": pipeline_new,
                "pipeline_updated": pipeline_updated,
                "research_queued": research_queued,
                "retries": retries,
                "transferred": len(processed),
                "discovered": len(work),
                "failed": len(failed_keys),
                "survivors_externalized": bool(ckpt.get("survivors_externalized")),
                "survivors": [],
            }
        )
        save_handoff_checkpoint(ckpt)
        if on_progress:
            on_progress(
                {
                    "phase": "PIPELINE_UPDATE",
                    "handoff_status": "IN_PROGRESS",
                    "discovered": len(work),
                    "transferred": len(processed),
                    "failed": len(failed_keys),
                    "pipeline_new": pipeline_new,
                    "pipeline_updated": pipeline_updated,
                    "deep_research_queued": research_queued,
                    "progress_percent": min(
                        94,
                        80 + int(14 * (len(processed) / max(1, len(work)))),
                    ),
                }
            )
        since_save = 0

    # First pass — skip already processed
    for idx, record in enumerate(work):
        cursor = idx + 1
        if not isinstance(record, dict):
            continue
        cid = identity_key(record)
        if cid in processed:
            continue
        try:
            ing = orch.ingest_discovery_record(record, persist=False)
            results.append(ing)
            processed.add(cid)
            if ing.get("created"):
                pipeline_new += 1
            else:
                pipeline_updated += 1
            if ing.get("survived") or ing.get("duplicate"):
                # duplicate may already be research-queued; count survivors newly queued
                if ing.get("survived"):
                    research_queued += 1
                elif ing.get("duplicate"):
                    row = store.get(cid)
                    if row and row.get("research_queued"):
                        pass
            since_save += 1
            _flush_progress()
        except Exception as exc:  # noqa: BLE001
            log.exception("Handoff upsert failed for %s", cid)
            failed_keys.append(cid)
            failed_upserts.append({"canonical_id": cid, "error": str(exc)[:300], "at": _utc()})
            since_save += 1
            _flush_progress()

    _flush_progress(force=True)

    # Fast reconcile using identity set (avoid full deepcopy of store.all when possible)
    recon = reconcile_handoff(survivors=work, store=store)
    recon["FAILED_UPSERTS"] = len(failed_upserts)
    elapsed = (now_utc() - t0).total_seconds()
    recon["records_per_sec"] = round(len(processed) / elapsed, 3) if elapsed > 0 else None
    recon["elapsed_seconds"] = round(elapsed, 2)
    recon["checkpoint_every"] = checkpoint_every
    attempt = 0
    while not recon["match"] and attempt < MAX_HANDOFF_RETRIES:
        attempt += 1
        retries += 1
        ckpt["status"] = "RETRYING"
        ckpt["retries"] = retries
        save_handoff_checkpoint(ckpt)
        if on_progress:
            on_progress(
                {
                    "phase": "PIPELINE_UPDATE",
                    "handoff_status": "RETRYING",
                    "discovered": len(work),
                    "transferred": len(processed),
                    "failed": recon["MISSING_FROM_PIPELINE"],
                    "retries": retries,
                }
            )
        missing_set = set(recon.get("missing_keys") or [])
        for record in work:
            if not isinstance(record, dict):
                continue
            cid = identity_key(record)
            if cid not in missing_set:
                continue
            try:
                ing = orch.ingest_discovery_record(record, persist=False)
                results.append(ing)
                processed.add(cid)
                if cid in failed_keys:
                    failed_keys = [k for k in failed_keys if k != cid]
                if ing.get("created"):
                    pipeline_new += 1
                else:
                    pipeline_updated += 1
            except Exception as exc:  # noqa: BLE001
                failed_upserts.append(
                    {"canonical_id": cid, "error": str(exc)[:300], "at": _utc(), "retry": attempt}
                )
        try:
            store.save()
        except Exception:
            log.exception("Handoff retry save failed")
        recon = reconcile_handoff(survivors=work, store=store)
        recon["FAILED_UPSERTS"] = len(failed_upserts)
        recon["retries"] = retries

    research_queue_count = sum(
        1 for r in store.all() if r.get("research_queued") or str(r.get("lifecycle") or "") == "RESEARCH_QUEUED"
    )
    recon["RESEARCH_QUEUE_COUNT"] = research_queue_count

    final_status = "COMPLETE" if recon["match"] else "MISMATCH"
    ckpt.update(
        {
            "status": final_status,
            "completed_at": _utc(),
            "cursor": len(work),
            "processed_keys": sorted(processed),
            "failed_keys": failed_keys,
            "failed_upserts": failed_upserts[-100:],
            "pipeline_new": pipeline_new,
            "pipeline_updated": pipeline_updated,
            "research_queued": research_queued,
            "retries": retries,
            "reconciliation": recon,
            "transferred": len(processed),
            "discovered": len(work),
            "failed": recon["MISSING_FROM_PIPELINE"],
        }
    )
    save_handoff_checkpoint(ckpt)
    try:
        store.save()
    except Exception:
        log.exception("Final handoff store.save failed")

    if on_progress:
        on_progress(
            {
                "phase": "PIPELINE_UPDATE",
                "handoff_status": final_status,
                "discovered": len(work),
                "transferred": recon["PIPELINE_COUNT"],
                "failed": recon["MISSING_FROM_PIPELINE"],
                "pipeline_new": pipeline_new,
                "pipeline_updated": pipeline_updated,
                "deep_research_queued": research_queued,
                "reconciliation": recon,
                "progress_percent": 94 if recon["match"] else 88,
            }
        )

    return {
        "kind": "M3DurableHandoffResult",
        "run_id": run_id,
        "status": final_status,
        "count": len(results),
        "pipeline_new": pipeline_new,
        "pipeline_updated": pipeline_updated,
        "research_queued": research_queued,
        "failed_upserts": failed_upserts,
        "retries": retries,
        "reconciliation": recon,
        "results": results,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def pending_handoff_for_resume() -> dict[str, Any] | None:
    ckpt = load_handoff_checkpoint()
    if not ckpt:
        return None
    if ckpt.get("status") in {"PENDING", "IN_PROGRESS", "RETRYING", "MISMATCH"} and ckpt.get("survivors"):
        return ckpt
    return None


def resume_incomplete_handoff(
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Called on boot — finish any checkpointed handoff left by restart/STALE."""
    ckpt = pending_handoff_for_resume()
    if not ckpt:
        return None
    run_id = str(ckpt.get("run_id") or "resume")
    survivors = list(ckpt.get("survivors") or [])
    print(
        f"govtracker: resuming incomplete pipeline handoff {run_id} survivors={len(survivors)}",
        flush=True,
    )
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore()
    try:
        store.reload_from_durable()
    except Exception:
        pass
    orch = M3EndToEndOrchestrator(store=store)
    return run_durable_handoff(
        run_id=run_id,
        survivors=survivors,
        store=store,
        orch=orch,
        discovery_metrics=ckpt.get("discovery_metrics"),
        on_progress=on_progress,
        resume=True,
    )
