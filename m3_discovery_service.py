"""M3 automatic production discovery — incremental, locked, feeds M3 pipeline.

Reuses: discovery.live_runner, discovery_checkpoint, M3EndToEndOrchestrator,
APScheduler (in-process with web), Cost Governor, tracked solicitation monitor.
Public solicitation retrieval only — no commercial outreach.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from national_discovery_funnel import stage1_ultra_cheap
from operating_mode import is_development_no_outreach, mode_snapshot

log = logging.getLogger("govtracker.m3_discovery")

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
STATUS_FAILED = "FAILED"
STATUS_STALE_RECOVERED = "STALE_RECOVERED"

TRIGGER_STARTUP = "STARTUP"
TRIGGER_SCHEDULED = "SCHEDULED"
TRIGGER_MANUAL = "MANUAL"
TRIGGER_CATCH_UP = "CATCH_UP"

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_discovery_run_state.json"
SETTINGS_KEY = "m3_discovery_run_state"
PIPELINE_SETTINGS_KEY = "m3_pipeline_store_v1"
STALE_HEARTBEAT_MINUTES = 20
FRESHNESS_MULTIPLIER = 1.5

_lock = threading.Lock()
_worker: threading.Thread | None = None


def _utc() -> str:
    return now_utc().isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def discovery_enabled() -> bool:
    return (os.environ.get("M3_DISCOVERY_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def discovery_interval_minutes() -> int:
    try:
        return max(5, int(os.environ.get("M3_DISCOVERY_INTERVAL_MINUTES") or "60"))
    except ValueError:
        return 60


def discovery_profile() -> str:
    return (os.environ.get("M3_DISCOVERY_PROFILE") or "broad").strip().lower() or "broad"


def _empty_state() -> dict[str, Any]:
    return {
        "kind": "M3DiscoveryRunState",
        "current_run": None,
        "last_successful_completion": None,
        "last_attempt": None,
        "next_scheduled_run": None,
        "lock": {"held": False, "run_id": None, "since": None},
        "updated_at": _utc(),
    }


def _load_state() -> dict[str, Any]:
    state = _empty_state()
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    state.update(data)
        finally:
            db.close()
    except Exception:
        pass
    if DEFAULT_PATH.exists():
        try:
            data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if not state.get("last_successful_completion") and not state.get("current_run"):
                    state.update(data)
        except Exception:
            pass
    return state


def _save_state(state: dict[str, Any]) -> None:
    state = deepcopy(state)
    state["updated_at"] = _utc()
    try:
        DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed writing discovery state file")
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            payload = json.dumps(state, default=str)
            if row:
                row.value = payload
            else:
                db.add(AppSetting(key=SETTINGS_KEY, value=payload))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Failed persisting discovery state to AppSetting")


def _persist_pipeline_store(store: Any) -> None:
    try:
        store.save()
        raw = store.path.read_text(encoding="utf-8") if store.path.exists() else ""
        if not raw:
            return
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=PIPELINE_SETTINGS_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Failed dual-writing M3 pipeline store")


def restore_pipeline_store_from_db(store: Any) -> bool:
    try:
        if store.path.exists():
            data = json.loads(store.path.read_text(encoding="utf-8"))
            if data.get("opportunities"):
                return False
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return False
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(row.value, encoding="utf-8")
            store._load()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("Failed restoring M3 pipeline from DB")
        return False


def _progress_for_phase(phase: str, sources_completed: int, sources_total: int) -> int:
    base = {
        "PREPARING": 2,
        "DISCOVERING": 10,
        "NORMALIZING": 55,
        "DEDUPLICATING": 60,
        "CHEAP_SCREENING": 70,
        "PIPELINE_UPDATE": 80,
        "TRACKED_CHANGE_CHECK": 90,
        "FINALIZING": 95,
    }.get(phase, 5)
    if phase == "DISCOVERING" and sources_total > 0:
        frac = min(1.0, sources_completed / max(1, sources_total))
        return min(54, int(10 + frac * 44))
    return base


def recover_stale_runs(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or _load_state()
    cur = state.get("current_run")
    if not cur or cur.get("status") not in {STATUS_RUNNING, STATUS_QUEUED}:
        return state
    hb = _parse(cur.get("last_heartbeat_at") or cur.get("started_at"))
    # QUEUED with no worker heartbeat recovers faster (Railway restart orphan)
    limit_min = 3 if cur.get("status") == STATUS_QUEUED else STALE_HEARTBEAT_MINUTES
    if hb and now_utc() - hb > timedelta(minutes=limit_min):
        cur["status"] = STATUS_STALE_RECOVERED
        cur["completed_at"] = _utc()
        cur["error_summary"] = (
            "Stale QUEUED/RUNNING lock recovered after missed heartbeat (likely process restart)"
        )
        cur["phase"] = "FINALIZING"
        state["last_attempt"] = deepcopy(cur)
        state["current_run"] = None
        state["lock"] = {"held": False, "run_id": None, "since": None}
        _save_state(state)
        log.warning("Recovered stale discovery run %s", cur.get("run_id"))
    return state


def compute_next_scheduled_run(from_time: datetime | None = None) -> str:
    from_time = from_time or now_utc()
    return (from_time + timedelta(minutes=discovery_interval_minutes())).isoformat()


def is_data_fresh(state: dict[str, Any] | None = None) -> bool:
    state = state or _load_state()
    last = _parse((state.get("last_successful_completion") or {}).get("completed_at"))
    if not last:
        return False
    threshold = timedelta(minutes=discovery_interval_minutes() * FRESHNESS_MULTIPLIER)
    return now_utc() - last <= threshold


def discovery_status() -> dict[str, Any]:
    state = recover_stale_runs()
    cur = state.get("current_run")
    last_ok = state.get("last_successful_completion")
    last_attempt = state.get("last_attempt")
    running = bool(cur and cur.get("status") in {STATUS_RUNNING, STATUS_QUEUED})
    fresh = is_data_fresh(state)
    next_run = state.get("next_scheduled_run") or compute_next_scheduled_run()
    status_label = "IDLE"
    if running:
        status_label = "RUNNING" if (cur or {}).get("status") == STATUS_RUNNING else "QUEUED"
    elif not last_ok:
        status_label = "NO_SUCCESSFUL_RUN"
    elif last_attempt and last_attempt.get("status") == STATUS_FAILED:
        la = _parse(last_attempt.get("completed_at"))
        lo = _parse((last_ok or {}).get("completed_at"))
        if la and (not lo or la > lo):
            status_label = "FAILED"
        elif not fresh:
            status_label = "STALE"
        else:
            status_label = "CURRENT"
    elif not fresh:
        status_label = "STALE"
    elif last_ok:
        status_label = "CURRENT"

    progress = 0
    if running:
        progress = int((cur or {}).get("progress_percent") or 0)
    elif fresh:
        progress = 100

    return {
        "kind": "M3DiscoveryStatus",
        "enabled": discovery_enabled(),
        "interval_minutes": discovery_interval_minutes(),
        "profile": discovery_profile(),
        "status": status_label,
        "running": running,
        "fresh": fresh,
        "progress_percent": progress,
        "phase": (cur or {}).get("phase") if running else None,
        "elapsed_hint_started_at": (cur or {}).get("started_at") if running else None,
        "current_run": cur,
        "last_successful_completion": last_ok,
        "last_attempt": last_attempt,
        "next_scheduled_run": next_run,
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
        "mode": mode_snapshot(),
        "commercial_outreach": False,
    }


def list_recent_runs(limit: int = 10) -> dict[str, Any]:
    state = _load_state()
    runs: list[dict[str, Any]] = []
    if state.get("current_run"):
        runs.append(state["current_run"])
    if state.get("last_attempt"):
        runs.append(state["last_attempt"])
    if state.get("last_successful_completion"):
        runs.append(state["last_successful_completion"])
    try:
        from database import SessionLocal
        from models import DiscoveryRun

        db = SessionLocal()
        try:
            rows = db.query(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(limit).all()
            for r in rows:
                runs.append(
                    {
                        "run_id": r.run_id,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "completed_at": r.finished_at.isoformat() if r.finished_at else None,
                        "sources_attempted": r.sources_attempted,
                        "sources_successful": r.sources_successful,
                        "sources_failed": r.sources_failed,
                        "metrics": r.metrics_json,
                    }
                )
        finally:
            db.close()
    except Exception:
        pass
    seen: set[str] = set()
    out = []
    for r in runs:
        rid = str(r.get("run_id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(r)
        if len(out) >= limit:
            break
    return {"runs": out, "count": len(out)}


def request_discovery_run(*, trigger_type: str = TRIGGER_MANUAL) -> dict[str, Any]:
    if not discovery_enabled() and trigger_type != TRIGGER_MANUAL:
        return {"accepted": False, "reason": "discovery_disabled"}

    with _lock:
        state = recover_stale_runs()
        cur = state.get("current_run")
        if cur and cur.get("status") in {STATUS_RUNNING, STATUS_QUEUED}:
            return {
                "accepted": False,
                "already_running": True,
                "message": "Discovery already running",
                "run": cur,
                "status": discovery_status(),
            }
        run_id = f"MDR-{uuid4().hex[:12]}"
        run = {
            "run_id": run_id,
            "trigger_type": trigger_type,
            "started_at": _utc(),
            "completed_at": None,
            "last_heartbeat_at": _utc(),
            "status": STATUS_RUNNING,
            "phase": "PREPARING",
            "progress_percent": 2,
            "sources_total": 0,
            "sources_attempted": 0,
            "sources_completed": 0,
            "sources_successful": 0,
            "sources_failed": 0,
            "records_retrieved": 0,
            "unique_records": 0,
            "open_current_records": 0,
            "product_screen_survivors": 0,
            "pipeline_new": 0,
            "pipeline_updated": 0,
            "pipeline_rejected": 0,
            "deep_research_queued": 0,
            "error_summary": None,
            "source_warnings": [],
        }
        state["current_run"] = run
        state["lock"] = {"held": True, "run_id": run_id, "since": _utc()}
        _save_state(state)

    _dispatch_discovery_job(run_id, trigger_type)
    return {"accepted": True, "already_running": False, "run_id": run_id, "status": discovery_status()}


def _dispatch_discovery_job(run_id: str, trigger_type: str) -> None:
    """Run discovery out-of-band in this web process (daemon thread).

    APScheduler interval handles cadence; one-shot execution uses a thread so
    RUN NOW / startup never depend on date-trigger misfire behavior.
    """
    global _worker
    _worker = threading.Thread(
        target=_execute_run,
        args=(run_id, trigger_type),
        name=f"m3-discovery-{run_id}",
        daemon=True,
    )
    _worker.start()
    log.info("Dispatched M3 discovery %s via thread (alive=%s)", run_id, _worker.is_alive())


def _update_run(run_id: str, **patch: Any) -> None:
    with _lock:
        state = _load_state()
        cur = state.get("current_run") or {}
        if cur.get("run_id") != run_id:
            return
        cur.update(patch)
        cur["last_heartbeat_at"] = _utc()
        cur["progress_percent"] = max(
            int(cur.get("progress_percent") or 0),
            _progress_for_phase(
                str(cur.get("phase") or "PREPARING"),
                int(cur.get("sources_completed") or 0),
                int(cur.get("sources_total") or 0),
            ),
        )
        state["current_run"] = cur
        _save_state(state)


def _finalize_run(run_id: str, *, status: str, error: str | None = None) -> None:
    with _lock:
        state = _load_state()
        cur = state.get("current_run") or {}
        if cur.get("run_id") != run_id:
            return
        cur["status"] = status
        cur["completed_at"] = _utc()
        cur["phase"] = "FINALIZING"
        cur["progress_percent"] = 100 if status.startswith("COMPLETED") else int(cur.get("progress_percent") or 0)
        if error:
            cur["error_summary"] = error
        state["last_attempt"] = deepcopy(cur)
        if status in {STATUS_COMPLETED, STATUS_COMPLETED_WITH_WARNINGS}:
            state["last_successful_completion"] = deepcopy(cur)
        state["current_run"] = None
        state["lock"] = {"held": False, "run_id": None, "since": None}
        state["next_scheduled_run"] = compute_next_scheduled_run()
        _save_state(state)


def _records_from_live_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    raw = (
        result.get("handoff_records")
        or result.get("opportunities")
        or result.get("collected")
        or result.get("records")
        or []
    )
    for item in raw:
        if not isinstance(item, dict):
            continue
        deadline = item.get("deadline_raw") or item.get("deadline")
        rd = item.get("response_deadline")
        if hasattr(rd, "isoformat"):
            deadline = deadline or rd.isoformat()
        rec = {
            "title": item.get("title"),
            "solicitation_number": item.get("solicitation_number") or item.get("external_id"),
            "external_id": item.get("external_id") or item.get("solicitation_number"),
            "agency": item.get("agency"),
            "source_id": item.get("source_id") or item.get("preferred_source_id"),
            "status": item.get("status") or "OPEN",
            "deadline": deadline,
            "detail_url": item.get("detail_url") or item.get("preferred_source_url"),
            "description": item.get("description"),
            "product_classification": item.get("product_classification") or item.get("classification"),
            "naics": item.get("naics"),
            "package_access": "PUBLIC",
            "source_modified_at": item.get("source_modified_at") or item.get("posted_at") or item.get("last_seen_at"),
        }
        if rec.get("title"):
            records.append(rec)
    return records


def _apply_incremental_filter(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer NEW/CHANGED vs last successful per-source checkpoint (failed sources do not advance)."""
    try:
        from discovery_checkpoint import filter_records_for_incremental, incremental_window
        from procurement_source_registry import ProcurementSourceRegistry

        reg = ProcurementSourceRegistry()
        _restore_registry_from_db(reg)
        out: list[dict[str, Any]] = []
        by_source: dict[str, list[dict[str, Any]]] = {}
        for r in records:
            by_source.setdefault(str(r.get("source_id") or "unknown"), []).append(r)
        for sid, rows in by_source.items():
            src = reg.get(sid) or {"source_id": sid, "last_successful_checkpoint": None}
            win = incremental_window(src)
            if "BOOTSTRAP" in str(win.get("mode") or "") or not win.get("since"):
                out.extend(rows)
            else:
                out.extend(filter_records_for_incremental(rows, since=win["since"]))
        return out
    except Exception:
        log.exception("Incremental filter skipped — using full handoff set")
        return records


REGISTRY_SETTINGS_KEY = "m3_procurement_source_registry_v1"


def _restore_registry_from_db(reg: Any) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == REGISTRY_SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return False
            reg.path.parent.mkdir(parents=True, exist_ok=True)
            reg.path.write_text(row.value, encoding="utf-8")
            reg._load()
            return True
        finally:
            db.close()
    except Exception:
        return False


def _persist_registry(reg: Any) -> None:
    try:
        reg.save()
        raw = reg.path.read_text(encoding="utf-8") if reg.path.exists() else ""
        if not raw:
            return
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == REGISTRY_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=REGISTRY_SETTINGS_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Failed dual-writing source registry")


def _check_tracked_changes(store: Any, survivors: list[dict[str, Any]]) -> int:
    """Reuse TrackedSolicitationMonitor + pipeline invalidate for active pursuits."""
    from tracked_solicitation import TrackedSolicitationMonitor, TrackedSolicitationStore

    tstore = TrackedSolicitationStore()
    mon = TrackedSolicitationMonitor(store=tstore)
    by_ext = {
        str(s.get("external_id") or s.get("solicitation_number") or "").strip().upper(): s
        for s in survivors
        if s.get("external_id") or s.get("solicitation_number")
    }
    events = 0
    for row in store.all():
        lc = str(row.get("lifecycle") or "")
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}:
            continue
        if not any(
            x in lc
            for x in ("PURSUIT", "PACKAGE", "COMMERCIAL", "FUNDING", "BID", "OPERATOR", "ACTIVE", "RESEARCH", "QUALIF")
        ):
            # Still check rows explicitly marked tracked
            if not row.get("tracked"):
                continue
        deal_id = str(row.get("deal_id") or row.get("canonical_id") or "")
        ext = str(row.get("external_id") or row.get("solicitation_number") or "").strip().upper()
        match = by_ext.get(ext)
        if not match:
            continue
        if not tstore.get(deal_id):
            from tracked_solicitation import INV_PURSUIT_WORTHY

            tstore.promote(deal_id, stage=INV_PURSUIT_WORTHY, meta={"from": "m3_discovery"})
        prev = {
            "deadline": row.get("deadline"),
            "status": row.get("status"),
            "specification_hash": row.get("evidence_fingerprint"),
            "quantity": (row.get("line_items") or [{}])[0].get("quantity")
            if isinstance(row.get("line_items"), list) and row.get("line_items")
            else row.get("quantity"),
        }
        cur = {
            "deadline": match.get("deadline"),
            "status": match.get("status"),
            "specification_hash": match.get("evidence_fingerprint")
            or match.get("description")
            or match.get("title"),
            "quantity": match.get("quantity"),
        }
        applied = mon.compare_versions(deal_id, prev, cur, test_only=False)
        if applied:
            events += len(applied)
            try:
                store.invalidate(
                    row.get("canonical_id") or deal_id,
                    change_type=applied[0].get("change_type") or "UNKNOWN_CHANGE",
                    affected=list(applied[0].get("affected_dependencies") or []),
                )
            except Exception:
                pass
    return events


def _execute_run(run_id: str, trigger_type: str) -> None:
    _update_run(run_id, status=STATUS_RUNNING, phase="PREPARING", progress_percent=2)
    try:
        from database import SessionLocal
        from discovery.live_runner import run_live_discovery
        from discovery.profiles import get_profile
        from m3_end_to_end import M3EndToEndOrchestrator
        from m3_pipeline_store import M3PipelineStore
        from models import DiscoveryRun
    except Exception as exc:
        log.exception("M3 discovery imports failed")
        _finalize_run(run_id, status=STATUS_FAILED, error=f"import_failure: {exc}")
        return

    _update_run(run_id, phase="PREPARING", progress_percent=5)
    session = None
    try:
        session = SessionLocal()
        try:
            from cost_governor import get_cost_governor

            # Touch governor — public discovery continues; paid research stays governed downstream
            get_cost_governor().dashboard_payload()
        except Exception:
            pass

        profile = discovery_profile()
        if profile not in {"tiny", "broad", "national"}:
            profile = "broad"
        # Catch-up / first-ever run may use national when configured default is broad
        if trigger_type in {TRIGGER_STARTUP, TRIGGER_CATCH_UP} and not _load_state().get(
            "last_successful_completion"
        ):
            profile = (os.environ.get("M3_DISCOVERY_BOOTSTRAP_PROFILE") or profile).strip().lower() or profile
            if profile not in {"tiny", "broad", "national"}:
                profile = "broad"

        try:
            planned = int(get_profile(profile).get("max_sources") or 0)
        except Exception:
            planned = 0
        _update_run(
            run_id,
            phase="DISCOVERING",
            progress_percent=10,
            sources_total=planned,
        )

        def _on_source(metrics: dict[str, Any], _sid: str) -> None:
            _update_run(
                run_id,
                phase="DISCOVERING",
                sources_total=max(planned, int(metrics.get("sources_attempted") or 0)),
                sources_attempted=int(metrics.get("sources_attempted") or 0),
                sources_completed=int(metrics.get("sources_attempted") or 0),
                sources_successful=int(metrics.get("sources_successful") or 0),
                sources_failed=int(metrics.get("sources_failed") or 0),
                records_retrieved=int(metrics.get("raw_records") or metrics.get("listing_records") or 0),
                unique_records=int(metrics.get("unique_records") or 0),
            )

        live = run_live_discovery(
            session,
            profile=profile,
            preview=False,
            persist=True,
            authorize_live=True,
            on_source_complete=_on_source,
        )
        if live.get("error"):
            _finalize_run(run_id, status=STATUS_FAILED, error=str(live.get("error")))
            return

        metrics = live.get("metrics") or {}
        sources_attempted = int(metrics.get("sources_attempted") or 0)
        sources_successful = int(metrics.get("sources_successful") or 0)
        sources_failed = int(metrics.get("sources_failed") or 0)
        _update_run(
            run_id,
            phase="NORMALIZING",
            sources_total=sources_attempted or planned,
            sources_attempted=sources_attempted,
            sources_completed=sources_attempted,
            sources_successful=sources_successful,
            sources_failed=sources_failed,
            records_retrieved=int(metrics.get("raw_records") or metrics.get("listing_records") or 0),
            unique_records=int(metrics.get("unique_records") or 0),
        )

        try:
            from procurement_source_registry import ProcurementSourceRegistry

            reg = ProcurementSourceRegistry()
            _restore_registry_from_db(reg)
            reg.apply_discovery_health(metrics.get("per_source") or {})
            _persist_registry(reg)
        except Exception:
            log.exception("Source checkpoint update failed")

        records = _apply_incremental_filter(_records_from_live_result(live))
        _update_run(run_id, phase="DEDUPLICATING", unique_records=len(records))
        open_current = [
            r
            for r in records
            if str(r.get("status") or "OPEN").upper()
            not in {"EXPIRED", "CANCELLED", "CANCELED", "AWARDED", "CLOSED"}
        ]
        _update_run(run_id, phase="CHEAP_SCREENING", open_current_records=len(open_current))

        survivors = []
        rejected = 0
        for r in open_current:
            screen = stage1_ultra_cheap(r)
            if screen.get("survive"):
                r["cheap_screen_survive"] = True
                r["product_classification"] = screen.get("classification") or r.get("product_classification")
                survivors.append(r)
            else:
                rejected += 1

        _update_run(
            run_id,
            phase="PIPELINE_UPDATE",
            product_screen_survivors=len(survivors),
            pipeline_rejected=rejected,
        )

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
        orch = M3EndToEndOrchestrator(store=store)
        # advance=True queues research but Cost Governor gates paid spend; no outreach
        batch = orch.run_from_discovery_batch(survivors, advance=True)
        pipeline_new = sum(1 for x in batch.get("results") or [] if x.get("created"))
        pipeline_updated = sum(
            1 for x in batch.get("results") or [] if x.get("duplicate") or (not x.get("created") and x.get("survived"))
        )
        deep_queued = int((batch.get("metrics") or {}).get("research_queued") or 0)
        _persist_pipeline_store(store)

        _update_run(
            run_id,
            phase="TRACKED_CHANGE_CHECK",
            pipeline_new=pipeline_new,
            pipeline_updated=pipeline_updated,
            deep_research_queued=deep_queued,
        )
        try:
            _check_tracked_changes(store, survivors)
            _persist_pipeline_store(store)
        except Exception:
            log.exception("Tracked solicitation check skipped")

        _update_run(run_id, phase="FINALIZING", progress_percent=98)
        try:
            db_run = DiscoveryRun(
                run_id=run_id,
                sources_attempted=sources_attempted,
                sources_successful=sources_successful,
                sources_failed=sources_failed,
                raw_notices_seen=int(metrics.get("raw_records") or 0),
                new_opportunities=pipeline_new,
                updated_opportunities=pipeline_updated,
                core_product_count=int(metrics.get("CORE_PRODUCT") or 0),
                product_plus_service_count=int(metrics.get("PRODUCT_PLUS_SERVICE") or 0),
                unknown_count=int(metrics.get("UNKNOWN") or 0),
                service_count=int(metrics.get("SERVICE") or 0),
                rejected_count=rejected,
                dry_run=False,
                notes=f"trigger={trigger_type};profile={profile}",
                metrics_json={
                    "trigger_type": trigger_type,
                    "profile": profile,
                    "product_screen_survivors": len(survivors),
                    "unique_records": len(records),
                    "open_current_records": len(open_current),
                    "deep_research_queued": deep_queued,
                    "LIVE_API_REQUESTS": live.get("LIVE_API_REQUESTS"),
                    "commercial_outreach": False,
                },
                finished_at=now_utc(),
            )
            session.add(db_run)
            session.commit()
        except Exception:
            log.exception("DiscoveryRun DB insert failed")
            session.rollback()

        final_status = STATUS_COMPLETED_WITH_WARNINGS if sources_failed > 0 else STATUS_COMPLETED
        _update_run(
            run_id,
            sources_successful=sources_successful,
            sources_failed=sources_failed,
            product_screen_survivors=len(survivors),
            pipeline_new=pipeline_new,
            pipeline_updated=pipeline_updated,
            pipeline_rejected=rejected,
            deep_research_queued=deep_queued,
            records_retrieved=int(metrics.get("raw_records") or len(records)),
            unique_records=len(records),
            open_current_records=len(open_current),
            source_warnings=[f"{sources_failed} source(s) failed"] if sources_failed else [],
        )
        _finalize_run(run_id, status=final_status)
        log.info(
            "M3 discovery %s done status=%s survivors=%s new=%s",
            run_id,
            final_status,
            len(survivors),
            pipeline_new,
        )
    except Exception as exc:
        log.exception("M3 discovery run failed")
        _finalize_run(run_id, status=STATUS_FAILED, error=str(exc))
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass



def maybe_startup_discovery() -> dict[str, Any]:
    if not discovery_enabled():
        return {"queued": False, "reason": "disabled"}
    state = recover_stale_runs()
    state["next_scheduled_run"] = compute_next_scheduled_run()
    _save_state(state)
    # Ensure pipeline restored on boot
    try:
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    # If a prior crash left a recovered attempt and data is still stale, catch up
    if state.get("current_run") and state["current_run"].get("status") in {
        STATUS_RUNNING,
        STATUS_QUEUED,
    }:
        return {"queued": False, "reason": "already_running", "status": discovery_status()}
    if is_data_fresh(state):
        return {"queued": False, "reason": "already_fresh", "status": discovery_status()}
    trigger = TRIGGER_STARTUP if not state.get("last_successful_completion") else TRIGGER_CATCH_UP
    return request_discovery_run(trigger_type=trigger)


def scheduled_discovery_tick() -> dict[str, Any]:
    if not discovery_enabled():
        return {"skipped": True, "reason": "disabled"}
    return request_discovery_run(trigger_type=TRIGGER_SCHEDULED)
