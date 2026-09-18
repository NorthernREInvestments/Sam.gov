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
PIPELINE_SETTINGS_KEY = "m3_pipeline_store_v1"  # kept for compatibility; store owns persistence

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
    """Legacy helper — schedule is now 06:00/14:00. Kept for callers; defaults to 480 (8h)."""
    try:
        return max(60, int(os.environ.get("M3_DISCOVERY_INTERVAL_MINUTES") or "480"))
    except ValueError:
        return 480


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


def is_data_fresh(state: dict[str, Any] | None = None) -> bool:
    state = state or _load_state()
    last = _parse((state.get("last_successful_completion") or {}).get("completed_at"))
    if not last:
        return False
    # Twice-daily cadence: treat fresh if completed within ~10 hours
    threshold = timedelta(hours=10)
    return now_utc() - last <= threshold


def _load_state_from_db() -> dict[str, Any] | None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                return data if isinstance(data, dict) else None
        finally:
            db.close()
    except Exception:
        return None
    return None


def _load_state() -> dict[str, Any]:
    state = _empty_state()
    db_data = _load_state_from_db()
    if db_data:
        state.update(db_data)
    if DEFAULT_PATH.exists():
        try:
            data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # File may be ephemeral; only fill gaps, never wipe durable success
                if not state.get("last_successful_completion") and data.get("last_successful_completion"):
                    state["last_successful_completion"] = data["last_successful_completion"]
                if not state.get("current_run") and not state.get("last_successful_completion") and not state.get("last_attempt"):
                    state.update(data)
        except Exception:
            pass
    return state


def _save_state(state: dict[str, Any]) -> None:
    state = deepcopy(state)
    # Never clobber a durable last_successful_completion with a partial write
    prior = _load_state_from_db() or {}
    if prior.get("last_successful_completion") and not state.get("last_successful_completion"):
        state["last_successful_completion"] = prior["last_successful_completion"]
    if (
        state.get("last_successful_completion")
        and prior.get("last_successful_completion")
        and not state.get("last_successful_completion", {}).get("completed_at")
        and prior["last_successful_completion"].get("completed_at")
    ):
        state["last_successful_completion"] = prior["last_successful_completion"]
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
        store.save()  # durable AppSetting + file when store.durable
    except Exception:
        log.exception("Failed dual-writing M3 pipeline store")


def restore_pipeline_store_from_db(store: Any) -> bool:
    """Reload authoritative pipeline from AppSetting into the store/file cache."""
    try:
        before = len(store.all())
        if hasattr(store, "reload_from_durable"):
            after = store.reload_from_durable()
            return after > before or after > 0
        # Legacy path
        from m3_pipeline_store import PIPELINE_SETTINGS_KEY, _read_durable_payload

        data = _read_durable_payload()
        if not data:
            return False
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        store._load()
        return True
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


def clear_orphan_locks_on_boot() -> dict[str, Any]:
    """Railway/uvicorn restart cannot keep an in-process discovery worker — clear locks."""
    state = _load_state()
    cur = state.get("current_run")
    if not cur or cur.get("status") not in {STATUS_RUNNING, STATUS_QUEUED}:
        return state
    cur = deepcopy(cur)
    cur["status"] = STATUS_STALE_RECOVERED
    cur["completed_at"] = _utc()
    cur["error_summary"] = "Orphan discovery lock cleared on process start"
    cur["phase"] = "FINALIZING"
    state["last_attempt"] = cur
    state["current_run"] = None
    state["lock"] = {"held": False, "run_id": None, "since": None}
    _save_state(state)
    print(f"govtracker: cleared orphan discovery lock {cur.get('run_id')}", flush=True)
    return state


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
        print(f"govtracker: recovered stale discovery {cur.get('run_id')}", flush=True)
    return state


def compute_next_scheduled_run(from_time: datetime | None = None) -> str:
    """Next 06:00 or 14:00 in configured scheduler timezone (not hourly)."""
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    from settings_store import get_scheduler_settings

    settings = get_scheduler_settings()
    try:
        tz = ZoneInfo(settings.get("timezone") or "America/Denver")
    except Exception:
        tz = ZoneInfo("America/Denver")
    from_time = from_time or now_utc()
    local = from_time.astimezone(tz)
    for day_offset in (0, 1, 2):
        day = (local + _td(days=day_offset)).replace(second=0, microsecond=0)
        for hour in (6, 14):
            run = day.replace(hour=hour, minute=0)
            if run > local:
                return run.astimezone(timezone.utc).isoformat()
    return (local + _td(days=1)).replace(hour=6, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


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

    focus = cur if running else (last_ok or last_attempt or {})
    recon = (focus or {}).get("reconciliation") or {}
    handoff_block = {
        "discovered": (focus or {}).get("handoff_discovered")
        or (focus or {}).get("product_screen_survivors")
        or recon.get("DISCOVERY_COUNT"),
        "transferred": (focus or {}).get("handoff_transferred") or recon.get("PIPELINE_COUNT"),
        "failed": (focus or {}).get("handoff_failed") or recon.get("MISSING_FROM_PIPELINE") or 0,
        "retrying": (focus or {}).get("handoff_status") == "RETRYING",
        "retries": (focus or {}).get("handoff_retries") or recon.get("retries") or 0,
        "status": (focus or {}).get("handoff_status")
        or ("COMPLETE" if (last_ok and not running) else None),
        "match": recon.get("match"),
        "MISSING_FROM_PIPELINE": recon.get("MISSING_FROM_PIPELINE"),
        "FAILED_UPSERTS": recon.get("FAILED_UPSERTS"),
        "DUPLICATE_HANDOFFS": recon.get("DUPLICATE_HANDOFFS"),
    }
    discovery_block = {
        "sources_attempted": (focus or {}).get("sources_attempted"),
        "sources_successful": (focus or {}).get("sources_successful"),
        "sources_failed": (focus or {}).get("sources_failed"),
        "records_fetched": (focus or {}).get("records_retrieved"),
        "unique_records": (focus or {}).get("unique_records"),
        "product_survivors": (focus or {}).get("product_screen_survivors"),
        "completed": (not running)
        and (focus or {}).get("status")
        in {STATUS_COMPLETED, STATUS_COMPLETED_WITH_WARNINGS, STATUS_STALE_RECOVERED},
        "failed": (focus or {}).get("status") == STATUS_FAILED,
        "status": (focus or {}).get("status"),
    }
    research_block = {
        "queued": (focus or {}).get("research_queue_count")
        or (focus or {}).get("deep_research_queued")
        or recon.get("RESEARCH_QUEUE_COUNT"),
        "processing": None,
        "completed": None,
    }
    try:
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        rows = store.all()
        research_block["queued"] = sum(
            1
            for r in rows
            if r.get("research_queued") or str(r.get("lifecycle") or "") in {"RESEARCH_QUEUED", "CHEAP_SCREENED"}
        )
        research_block["processing"] = sum(
            1 for r in rows if r.get("research_in_progress") or str(r.get("lifecycle") or "") == "RESEARCH_IN_PROGRESS"
        )
        research_block["completed"] = sum(
            1
            for r in rows
            if str(r.get("lifecycle") or "")
            not in {
                "RESEARCH_QUEUED",
                "RESEARCH_IN_PROGRESS",
                "CHEAP_SCREENED",
                "DISCOVERED",
                "REJECTED",
                "REJECTED_CHEAP_SCREEN",
            }
            and not r.get("research_queued")
        )
    except Exception:
        pass

    pending_ckpt = None
    try:
        from m3_pipeline_handoff import pending_handoff_for_resume

        pending_ckpt = pending_handoff_for_resume()
    except Exception:
        pass

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
        "DISCOVERY": discovery_block,
        "PIPELINE_HANDOFF": handoff_block,
        "RESEARCH": research_block,
        "pending_handoff_resume": bool(pending_ckpt),
        "pending_handoff_run_id": (pending_ckpt or {}).get("run_id"),
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


def request_discovery_run(
    *,
    trigger_type: str = TRIGGER_MANUAL,
    profile: str | None = None,
    bootstrap: bool = False,
) -> dict[str, Any]:
    if not discovery_enabled() and trigger_type != TRIGGER_MANUAL:
        return {"accepted": False, "reason": "discovery_disabled"}

    profile_override = None
    if bootstrap:
        profile_override = "national"
    elif profile:
        p = str(profile).strip().lower()
        if p in {"tiny", "broad", "national"}:
            profile_override = p

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
            "profile_override": profile_override,
            "bootstrap": bool(bootstrap or profile_override == "national"),
        }
        state["current_run"] = run
        state["lock"] = {"held": True, "run_id": run_id, "since": _utc()}
        _save_state(state)

    _dispatch_discovery_job(run_id, trigger_type)
    return {
        "accepted": True,
        "already_running": False,
        "run_id": run_id,
        "profile": profile_override or discovery_profile(),
        "bootstrap": bool(bootstrap or profile_override == "national"),
        "status": discovery_status(),
    }


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
    print(f"govtracker: discovery thread started {run_id} alive={_worker.is_alive()}", flush=True)
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


def _records_from_live_result(result: dict[str, Any], *, discovery_run_id: str | None = None) -> list[dict[str, Any]]:
    """Normalize discovery survivors while preserving raw evidence for the chain."""
    from m3_evidence_chain import enrich_discovery_record_for_pipeline

    records = []
    raw = (
        result.get("handoff_records")
        or result.get("opportunities")
        or result.get("collected")
        or result.get("records")
        or []
    )
    run_id = discovery_run_id or result.get("run_id")
    for item in raw:
        if not isinstance(item, dict):
            continue
        deadline = item.get("deadline_raw") or item.get("deadline")
        rd = item.get("response_deadline")
        if hasattr(rd, "isoformat"):
            deadline = deadline or rd.isoformat()
        # Start from full item so document links / raw metadata are not stripped
        rec = dict(item)
        rec.update(
            {
                "title": item.get("title"),
                "solicitation_number": item.get("solicitation_number") or item.get("external_id"),
                "external_id": item.get("external_id") or item.get("solicitation_number"),
                "agency": item.get("agency"),
                "source_id": item.get("source_id") or item.get("preferred_source_id"),
                "status": item.get("status") or "OPEN",
                "deadline": deadline,
                "detail_url": item.get("detail_url") or item.get("preferred_source_url") or item.get("source_url") or item.get("url"),
                "description": item.get("description"),
                "product_classification": item.get("product_classification") or item.get("classification"),
                "naics": item.get("naics"),
                "package_access": item.get("package_access") or item.get("document_access") or "PUBLIC",
                "source_modified_at": item.get("source_modified_at") or item.get("posted_at") or item.get("last_seen_at"),
            }
        )
        if rec.get("title"):
            records.append(enrich_discovery_record_for_pipeline(rec, discovery_run_id=run_id))
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
    print(f"govtracker: discovery execute begin {run_id} trigger={trigger_type}", flush=True)
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
        print(f"govtracker: discovery import failure {exc}", flush=True)
        _finalize_run(run_id, status=STATUS_FAILED, error=f"import_failure: {exc}")
        return

    print(f"govtracker: discovery imports ok {run_id}", flush=True)
    _update_run(run_id, phase="PREPARING", progress_percent=5)
    session = None
    try:
        print(f"govtracker: discovery opening DB session {run_id}", flush=True)
        session = SessionLocal()
        print(f"govtracker: discovery DB session open {run_id}", flush=True)
        _update_run(run_id, phase="PREPARING", progress_percent=8)
        try:
            from cost_governor import get_cost_governor

            get_cost_governor().dashboard_payload()
        except Exception:
            pass
        profile = discovery_profile()
        # Operator/bootstrap override wins over env default
        try:
            ov = (_load_state().get("current_run") or {}).get("profile_override")
            if ov in {"tiny", "broad", "national"}:
                profile = ov
        except Exception:
            pass
        if profile not in {"tiny", "broad", "national"}:
            profile = "broad"
        # Catch-up / first-ever run: NATIONAL market bootstrap (full eligible + pagination exhaust)
        if trigger_type in {TRIGGER_STARTUP, TRIGGER_CATCH_UP} and not _load_state().get(
            "last_successful_completion"
        ):
            if not (_load_state().get("current_run") or {}).get("profile_override"):
                profile = (
                    os.environ.get("M3_DISCOVERY_BOOTSTRAP_PROFILE") or "national"
                ).strip().lower() or "national"
                if profile not in {"tiny", "broad", "national"}:
                    profile = "national"

        try:
            from discovery.selection import select_all_eligible_sources

            prof = get_profile(profile)
            if prof.get("all_eligible_sources") or prof.get("max_sources") is None:
                planned = int(select_all_eligible_sources(include_blocked_accounted=False)["eligible_count"])
            else:
                planned = int(prof.get("max_sources") or 0)
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

        records = _apply_incremental_filter(_records_from_live_result(live, discovery_run_id=run_id))
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
            handoff_status="PENDING",
            handoff_discovered=len(survivors),
            handoff_transferred=0,
            handoff_failed=0,
        )

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
        orch = M3EndToEndOrchestrator(store=store)

        def _on_handoff_progress(patch: dict[str, Any]) -> None:
            _update_run(
                run_id,
                phase=patch.get("phase") or "PIPELINE_UPDATE",
                progress_percent=patch.get("progress_percent"),
                handoff_status=patch.get("handoff_status"),
                handoff_discovered=patch.get("discovered"),
                handoff_transferred=patch.get("transferred"),
                handoff_failed=patch.get("failed"),
                handoff_retries=patch.get("retries"),
                pipeline_new=patch.get("pipeline_new"),
                pipeline_updated=patch.get("pipeline_updated"),
                deep_research_queued=patch.get("deep_research_queued"),
                reconciliation=patch.get("reconciliation"),
            )

        from m3_pipeline_handoff import run_durable_handoff

        # Durable checkpoint → idempotent upserts → reconcile (no advance during discovery)
        batch = run_durable_handoff(
            run_id=run_id,
            survivors=survivors,
            store=store,
            orch=orch,
            discovery_metrics=metrics,
            on_progress=_on_handoff_progress,
            resume=True,
        )
        pipeline_new = int(batch.get("pipeline_new") or 0)
        pipeline_updated = int(batch.get("pipeline_updated") or 0)
        deep_queued = int(batch.get("research_queued") or 0)
        recon = batch.get("reconciliation") or {}
        _persist_pipeline_store(store)

        _update_run(
            run_id,
            phase="TRACKED_CHANGE_CHECK",
            pipeline_new=pipeline_new,
            pipeline_updated=pipeline_updated,
            deep_research_queued=deep_queued,
            handoff_status=batch.get("status"),
            handoff_discovered=int(recon.get("DISCOVERY_COUNT") or len(survivors)),
            handoff_transferred=int(recon.get("PIPELINE_COUNT") or 0),
            handoff_failed=int(recon.get("MISSING_FROM_PIPELINE") or 0),
            handoff_retries=int(batch.get("retries") or 0),
            reconciliation=recon,
            research_queue_count=int(recon.get("RESEARCH_QUEUE_COUNT") or deep_queued),
        )
        try:
            _check_tracked_changes(store, survivors)
            _persist_pipeline_store(store)
        except Exception:
            log.exception("Tracked solicitation check skipped")

        _update_run(run_id, phase="FINALIZING", progress_percent=98)
        completeness = live.get("completeness") or {}
        run_status = live.get("run_status") or "COMPLETE"
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
                notes=f"trigger={trigger_type};profile={profile};run_status={run_status};handoff={batch.get('status')}",
                metrics_json={
                    "trigger_type": trigger_type,
                    "profile": profile,
                    "product_screen_survivors": len(survivors),
                    "unique_records": len(records),
                    "open_current_records": len(open_current),
                    "deep_research_queued": deep_queued,
                    "LIVE_API_REQUESTS": live.get("LIVE_API_REQUESTS"),
                    "commercial_outreach": False,
                    "completeness": completeness,
                    "run_status": run_status,
                    "partial_reason": live.get("partial_reason"),
                    "fetched_this_run": metrics.get("fetched_this_run"),
                    "known_active_market_inventory": metrics.get("known_active_market_inventory"),
                    "pages_fetched_total": metrics.get("pages_fetched_total"),
                    "eligible_sources": metrics.get("eligible_sources"),
                    "registered_sources": metrics.get("registered_sources"),
                    "unattempted_eligible": metrics.get("unattempted_eligible"),
                    "OpenAI": live.get("OpenAI") or 0,
                    "paid": live.get("paid") or 0,
                    "SAM": live.get("SAM") or 0,
                    "dla_fallback": live.get("dla_fallback"),
                    "productive_discovery_sources": (live.get("completeness") or {}).get(
                        "productive_discovery_sources"
                    ),
                    "authoritative_productive_sources": (live.get("completeness") or {}).get(
                        "authoritative_productive_sources"
                    ),
                    "handoff": {
                        "status": batch.get("status"),
                        "reconciliation": recon,
                        "retries": batch.get("retries"),
                        "failed_upserts": len(batch.get("failed_upserts") or []),
                    },
                },
                finished_at=now_utc(),
            )
            session.add(db_run)
            session.commit()
        except Exception:
            log.exception("DiscoveryRun DB insert failed")
            session.rollback()

        handoff_ok = batch.get("status") == "COMPLETE" and bool(recon.get("match", False))
        if not handoff_ok:
            # Do not silently succeed — expose mismatch and keep checkpoint for resume
            final_status = STATUS_COMPLETED_WITH_WARNINGS
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
                handoff_status="MISMATCH",
                reconciliation=recon,
                error_summary=(
                    f"PIPELINE_HANDOFF_MISMATCH discovery={recon.get('DISCOVERY_COUNT')} "
                    f"pipeline={recon.get('PIPELINE_COUNT')} missing={recon.get('MISSING_FROM_PIPELINE')}"
                ),
                source_warnings=[
                    f"HANDOFF_MISMATCH missing={recon.get('MISSING_FROM_PIPELINE')}",
                    *(
                        [f"PARTIAL: {live.get('partial_reason')}"]
                        if run_status == "PARTIAL_DISCOVERY_RUN"
                        else ([f"{sources_failed} source(s) failed"] if sources_failed else [])
                    ),
                ],
            )
            _finalize_run(
                run_id,
                status=final_status,
                error=(
                    f"PIPELINE_HANDOFF_MISMATCH discovery={recon.get('DISCOVERY_COUNT')} "
                    f"pipeline={recon.get('PIPELINE_COUNT')} missing={recon.get('MISSING_FROM_PIPELINE')}"
                ),
            )
            log.warning(
                "M3 discovery %s handoff mismatch discovery=%s pipeline=%s",
                run_id,
                recon.get("DISCOVERY_COUNT"),
                recon.get("PIPELINE_COUNT"),
            )
            return

        if run_status == "PARTIAL_DISCOVERY_RUN":
            final_status = STATUS_COMPLETED_WITH_WARNINGS
        elif sources_failed > 0:
            final_status = STATUS_COMPLETED_WITH_WARNINGS
        else:
            final_status = STATUS_COMPLETED
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
            handoff_status="COMPLETE",
            handoff_discovered=int(recon.get("DISCOVERY_COUNT") or len(survivors)),
            handoff_transferred=int(recon.get("PIPELINE_COUNT") or 0),
            handoff_failed=0,
            reconciliation=recon,
            research_queue_count=int(recon.get("RESEARCH_QUEUE_COUNT") or deep_queued),
            per_source_summary={
                sid: {
                    "ok": (row or {}).get("ok"),
                    "raw": (row or {}).get("raw") or (row or {}).get("records_fetched"),
                    "unique": (row or {}).get("unique"),
                    "source_stop_reason": (row or {}).get("source_stop_reason"),
                    "root_cause": (row or {}).get("root_cause"),
                    "access_outcome": (row or {}).get("access_outcome"),
                }
                for sid, row in (metrics.get("per_source") or {}).items()
                if isinstance(row, dict)
            },
            source_warnings=(
                [f"PARTIAL: {live.get('partial_reason')}"]
                if run_status == "PARTIAL_DISCOVERY_RUN"
                else ([f"{sources_failed} source(s) failed"] if sources_failed else [])
            ),
        )
        # Clear durable survivors only after verified match
        try:
            from m3_pipeline_handoff import clear_handoff_checkpoint

            clear_handoff_checkpoint()
        except Exception:
            log.exception("Failed clearing handoff checkpoint")
        _finalize_run(run_id, status=final_status)
        log.info(
            "M3 discovery %s done status=%s survivors=%s new=%s transferred=%s",
            run_id,
            final_status,
            len(survivors),
            pipeline_new,
            recon.get("PIPELINE_COUNT"),
        )
        # Drain RESEARCH_QUEUED automatically after discovery feeds the pipeline
        try:
            from m3_research_service import maybe_request_research_after_discovery

            maybe_request_research_after_discovery()
        except Exception:
            log.exception("Post-discovery research kick failed")
        # Commercial intelligence on top candidates (research only — no outreach)
        try:
            from m3_commercial_engine import analyze_top_commercial_opportunities
            from m3_pipeline_store import M3PipelineStore

            cstore = M3PipelineStore()
            restore_pipeline_store_from_db(cstore)
            analyze_top_commercial_opportunities(cstore, limit=25)
        except Exception:
            log.exception("Post-discovery commercial analysis failed")
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
    # Process restart ⇒ any prior RUNNING/QUEUED lock is orphaned
    state = clear_orphan_locks_on_boot()
    state["next_scheduled_run"] = compute_next_scheduled_run()
    _save_state(state)
    # Ensure pipeline restored on boot
    try:
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    # Resume incomplete handoff before starting a new discovery run
    try:
        from m3_pipeline_handoff import resume_incomplete_handoff

        resumed = resume_incomplete_handoff()
        if resumed:
            print(
                f"govtracker: boot resumed handoff status={resumed.get('status')} "
                f"match={(resumed.get('reconciliation') or {}).get('match')}",
                flush=True,
            )
    except Exception:
        log.exception("Boot handoff resume failed")
    if is_data_fresh(state):
        print("govtracker: discovery data already fresh — skip startup run", flush=True)
        return {"queued": False, "reason": "already_fresh", "status": discovery_status()}
    trigger = TRIGGER_STARTUP if not state.get("last_successful_completion") else TRIGGER_CATCH_UP
    print(f"govtracker: startup discovery trigger={trigger}", flush=True)
    return request_discovery_run(trigger_type=trigger)


def scheduled_discovery_tick() -> dict[str, Any]:
    if not discovery_enabled():
        return {"skipped": True, "reason": "disabled"}
    return request_discovery_run(trigger_type=TRIGGER_SCHEDULED)
