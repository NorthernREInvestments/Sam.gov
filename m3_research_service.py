"""M3 automatic research queue drain — durable progress, Cost Governor, no outreach.

Reuses M3EndToEndOrchestrator.advance / lifecycle / pipeline store.
Does not duplicate research engines. Processes RESEARCH_QUEUED / QUEUE_RESEARCH
automatically in the web process (daemon thread + APScheduler tick).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from m3_lifecycle import (
    LC_ECONOMICS_UNATTRACTIVE,
    LC_REJECTED,
    LC_REJECTED_CHEAP,
    NA_QUEUE_RESEARCH,
    NA_WAIT_BUDGET,
    determine_next_action,
    derive_lifecycle,
)
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, is_development_no_outreach, mode_snapshot, set_operating_mode

log = logging.getLogger("govtracker.m3_research")

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
STATUS_FAILED = "FAILED"
STATUS_STALE_RECOVERED = "STALE_RECOVERED"
STATUS_PAUSED_BUDGET = "PAUSED_BUDGET"

TRIGGER_STARTUP = "STARTUP"
TRIGGER_SCHEDULED = "SCHEDULED"
TRIGGER_MANUAL = "MANUAL"
TRIGGER_CATCH_UP = "CATCH_UP"
TRIGGER_AFTER_DISCOVERY = "AFTER_DISCOVERY"

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_research_run_state.json"
SETTINGS_KEY = "m3_research_run_state"

STALE_HEARTBEAT_MINUTES = 15
ETA_MIN_SAMPLES = 3

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


def research_enabled() -> bool:
    return (os.environ.get("M3_RESEARCH_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def research_interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("M3_RESEARCH_INTERVAL_MINUTES") or "5"))
    except ValueError:
        return 5


def research_max_per_run() -> int:
    try:
        return max(1, int(os.environ.get("M3_RESEARCH_MAX_PER_RUN") or "200"))
    except ValueError:
        return 200


def _empty_state() -> dict[str, Any]:
    return {
        "kind": "M3ResearchRunState",
        "current_run": None,
        "last_successful_completion": None,
        "last_attempt": None,
        "next_scheduled_run": None,
        "lock": {"held": False, "run_id": None, "since": None},
        "updated_at": _utc(),
    }


def _empty_run(run_id: str, trigger_type: str, *, total_candidates: int = 0) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "trigger_type": trigger_type,
        "status": STATUS_RUNNING,
        "started_at": _utc(),
        "completed_at": None,
        "heartbeat_at": _utc(),
        "last_heartbeat_at": _utc(),
        "total_candidates": total_candidates,
        "queued": total_candidates,
        "currently_processing": None,
        "currently_processing_title": None,
        "processed": 0,
        "rejected": 0,
        "advanced": 0,
        "deferred": 0,
        "failed": 0,
        "paid_actions_used": 0,
        "actual_external_spend": 0.0,
        "progress_percent": 0,
        "eta_seconds": None,
        "eta_label": "Estimating...",
        "throughput_seconds": [],
        "processed_ids": [],
        "error_summary": None,
        "idle_reason": None,
        "spend_baseline_absolute": None,
        "spend_baseline_paid_actions": None,
    }


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
                if not state.get("last_successful_completion") and data.get("last_successful_completion"):
                    state["last_successful_completion"] = data["last_successful_completion"]
                if (
                    not state.get("current_run")
                    and not state.get("last_successful_completion")
                    and not state.get("last_attempt")
                ):
                    state.update(data)
        except Exception:
            pass
    return state


def _save_state(state: dict[str, Any]) -> None:
    state = deepcopy(state)
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
        log.exception("Failed writing research state file")
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
        log.exception("Failed persisting research state to AppSetting")


def clear_orphan_locks_on_boot() -> dict[str, Any]:
    """Railway/uvicorn restart cannot keep an in-process research worker — clear locks."""
    state = _load_state()
    cur = state.get("current_run")
    if not cur or cur.get("status") not in {STATUS_RUNNING, STATUS_QUEUED}:
        return state
    cur = deepcopy(cur)
    cur["status"] = STATUS_STALE_RECOVERED
    cur["completed_at"] = _utc()
    cur["error_summary"] = "Orphan research lock cleared on process start — queue will resume"
    cur["currently_processing"] = None
    cur["currently_processing_title"] = None
    state["last_attempt"] = cur
    state["current_run"] = None
    state["lock"] = {"held": False, "run_id": None, "since": None}
    _save_state(state)
    print(f"govtracker: cleared orphan research lock {cur.get('run_id')}", flush=True)
    return state


def recover_stale_runs(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or _load_state()
    cur = state.get("current_run")
    if not cur or cur.get("status") not in {STATUS_RUNNING, STATUS_QUEUED}:
        return state
    hb = _parse(cur.get("heartbeat_at") or cur.get("last_heartbeat_at") or cur.get("started_at"))
    limit_min = 3 if cur.get("status") == STATUS_QUEUED else STALE_HEARTBEAT_MINUTES
    if hb and now_utc() - hb > timedelta(minutes=limit_min):
        cur = deepcopy(cur)
        cur["status"] = STATUS_STALE_RECOVERED
        cur["completed_at"] = _utc()
        cur["error_summary"] = (
            "Stale QUEUED/RUNNING research lock recovered after missed heartbeat (likely process restart)"
        )
        cur["currently_processing"] = None
        cur["currently_processing_title"] = None
        state["last_attempt"] = cur
        state["current_run"] = None
        state["lock"] = {"held": False, "run_id": None, "since": None}
        _save_state(state)
        log.warning("Recovered stale research run %s", cur.get("run_id"))
        print(f"govtracker: recovered stale research {cur.get('run_id')}", flush=True)
    return state


def compute_next_scheduled_run(from_time: datetime | None = None) -> str:
    from_time = from_time or now_utc()
    return (from_time + timedelta(minutes=research_interval_minutes())).isoformat()


def _next_action_code(row: dict[str, Any]) -> str:
    nxt = row.get("pending_next_action")
    if isinstance(nxt, dict):
        return str(nxt.get("next_action") or "")
    if isinstance(nxt, str) and nxt:
        return nxt
    return str((determine_next_action(row) or {}).get("next_action") or "")


def _is_terminal_rejected(row: dict[str, Any]) -> bool:
    lc = str(row.get("lifecycle") or derive_lifecycle(row) or "")
    return lc in {LC_REJECTED, LC_REJECTED_CHEAP, LC_ECONOMICS_UNATTRACTIVE} or bool(row.get("rejected"))


def _needs_research(row: dict[str, Any]) -> bool:
    """True when automatic research should (re)run for this opportunity."""
    lc = str(row.get("lifecycle") or derive_lifecycle(row) or "")
    if lc in {
        LC_REJECTED,
        LC_REJECTED_CHEAP,
        LC_ECONOMICS_UNATTRACTIVE,
        "CANCELLED",
        "CLOSED",
        "AWARDED",
        "ARCHIVED",
        "LOST",
    }:
        return False

    action = _next_action_code(row)
    fp = row.get("evidence_fingerprint")
    completed_fp = row.get("research_completed_fingerprint")
    last_at = _parse(row.get("research_last_attempt_at"))
    inv_at = _parse((row.get("invalidation") or {}).get("at"))
    invalidated_since = bool(inv_at and (not last_at or inv_at > last_at))
    evidence_inv = _parse(row.get("evidence_invalidated_at"))
    evidence_invalidated = bool(evidence_inv and (not last_at or evidence_inv > last_at))

    # Evidence ladder incomplete → allow acquisition-driven reprocess even if researched
    ea = row.get("evidence_acquisition") or {}
    tiers = ea.get("tiers_attempted") or []
    ladder_incomplete = False
    if lc in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS", "CHEAP_SCREENED", "PACKAGE_REQUIRED"}:
        if not tiers or "TIER_1_DIRECT" not in tiers:
            ladder_incomplete = True

    # Idempotency: same evidence fingerprint already researched → skip unless invalidated / ladder gap
    if (
        fp
        and completed_fp
        and completed_fp == fp
        and not invalidated_since
        and not evidence_invalidated
        and not ladder_incomplete
        and not row.get("research_in_progress")
    ):
        return False

    if ladder_incomplete or evidence_invalidated:
        return True
    if row.get("research_in_progress"):
        return True
    if action == NA_QUEUE_RESEARCH:
        return True
    if action == "AUTO_CONTINUE" and lc in {
        "RESEARCH_QUEUED",
        "RESEARCH_IN_PROGRESS",
        "PACKAGE_REQUIRED",
        "PACKAGE_ACQUIRED",
        "REQUIREMENTS_PARSED",
        "BOM_READY",
        "ECONOMICS_IN_PROGRESS",
        "CHEAP_SCREENED",
    }:
        return True
    if action == NA_WAIT_BUDGET and lc in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS", "CHEAP_SCREENED"}:
        return True
    if lc in {"RESEARCH_QUEUED", "CHEAP_SCREENED"} and not completed_fp:
        return True
    return False

def _priority_tuple(row: dict[str, Any]) -> tuple:
    """Priority: serious pursuits → finish started → existing backlog → new discovery."""
    serious = 0
    if row.get("serious_pursuit") or row.get("first_pursuit") or row.get("controlled_pursuit"):
        serious = 0
    elif row.get("operator_priority") in {"HIGH", "SERIOUS", 1}:
        serious = 0
    else:
        serious = 1

    in_progress = 0 if row.get("research_in_progress") else 1
    # Existing backlog (older ingest) before freshly discovered
    created = str(row.get("created_at") or row.get("ingested_at") or row.get("updated_at") or "9999")
    return (serious, in_progress, created, str(row.get("canonical_id") or ""))


def list_research_candidates(store: Any | None = None) -> list[dict[str, Any]]:
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = store or M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    out = []
    for row in store.all():
        if _needs_research(row):
            out.append(row)
    out.sort(key=_priority_tuple)
    return out


def _classify_outcome(before: dict[str, Any], after: dict[str, Any] | None, *, error: str | None = None) -> str:
    if error:
        return "failed"
    if not after:
        return "failed"
    if _is_terminal_rejected(after):
        return "rejected"
    before_lc = str(before.get("lifecycle") or "")
    after_lc = str(after.get("lifecycle") or derive_lifecycle(after) or "")
    after_action = _next_action_code(after)
    advanced_markers = {
        "PACKAGE_ACQUIRED",
        "REQUIREMENTS_PARSED",
        "BOM_READY",
        "ECONOMICS_IN_PROGRESS",
        "ECONOMICS_PRELIMINARY",
        "ECONOMICS_ATTRACTIVE",
        "COMPLIANCE_IN_PROGRESS",
        "PRICING_IN_PROGRESS",
        "COMMERCIAL_VERIFICATION_REQUIRED",
        "FUNDING_VERIFICATION_REQUIRED",
        "DRAFT_BID_READY",
        "READY_FOR_OPERATOR_ACTION",
        "PACKAGE_ACCESS_GATED",
    }
    if after_lc in advanced_markers and after_lc != before_lc:
        return "advanced"
    if after_action in {
        "READY_FOR_OPERATOR_ACTION",
        "WAIT_COMMERCIAL_VERIFICATION",
        "WAIT_FUNDING_VERIFICATION",
        "WAIT_OPERATOR",
        "WAIT_COMPLIANCE_RESOLUTION",
    }:
        return "advanced"
    if after_action == NA_WAIT_BUDGET:
        return "deferred"
    if after_action in {"WAIT_PACKAGE", "WAIT_PUBLIC_EVIDENCE", NA_QUEUE_RESEARCH}:
        return "deferred"
    if after_lc in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS", "PACKAGE_REQUIRED", "CHEAP_SCREENED"}:
        return "deferred"
    if after_lc != before_lc and after_lc not in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS"}:
        return "advanced"
    return "deferred"


def _governor_snapshot() -> dict[str, Any]:
    try:
        from cost_governor import get_cost_governor

        dash = get_cost_governor().dashboard_payload()
        return {
            "absolute_used": float(dash.get("absolute_used") or dash.get("today_spend") or 0),
            "today_spend": float(dash.get("today_spend") or 0),
            "paid_actions": int(dash.get("deferred_paid_actions") or 0),
            "run_spent": float((dash.get("run_spent") if "run_spent" in dash else 0) or 0),
        }
    except Exception:
        return {"absolute_used": 0.0, "today_spend": 0.0, "paid_actions": 0, "run_spent": 0.0}


def _paid_actions_count() -> int:
    try:
        from cost_governor import get_cost_governor

        gov = get_cost_governor()
        ledger = getattr(gov, "_ledger", None) or getattr(gov, "ledger", None) or []
        if isinstance(ledger, list):
            return len(ledger)
        snap = gov.budget_snapshot() if hasattr(gov, "budget_snapshot") else {}
        totals = snap.get("totals") or {}
        return int(totals.get("actions") or totals.get("paid_actions") or 0)
    except Exception:
        return 0


def _update_run(run_id: str, **patch: Any) -> None:
    with _lock:
        state = _load_state()
        cur = state.get("current_run") or {}
        if cur.get("run_id") != run_id:
            return
        cur.update(patch)
        cur["heartbeat_at"] = _utc()
        cur["last_heartbeat_at"] = cur["heartbeat_at"]
        total = max(1, int(cur.get("total_candidates") or 1))
        processed = int(cur.get("processed") or 0)
        cur["progress_percent"] = min(100, int(round(100.0 * processed / total)))
        cur["queued"] = max(0, int(cur.get("total_candidates") or 0) - processed)
        # ETA from observed throughput
        samples = list(cur.get("throughput_seconds") or [])
        remaining = max(0, int(cur.get("total_candidates") or 0) - processed)
        if len(samples) >= ETA_MIN_SAMPLES and remaining > 0:
            avg = sum(samples) / len(samples)
            eta = int(round(avg * remaining))
            cur["eta_seconds"] = eta
            cur["eta_label"] = _fmt_eta(eta)
        elif remaining <= 0:
            cur["eta_seconds"] = 0
            cur["eta_label"] = "0s"
        else:
            cur["eta_seconds"] = None
            cur["eta_label"] = "Estimating..."
        state["current_run"] = cur
        _save_state(state)


def _fmt_eta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"~{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"~{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"~{h}h {m:02d}m"


def _finalize_run(run_id: str, *, status: str, error: str | None = None) -> None:
    with _lock:
        state = _load_state()
        cur = state.get("current_run") or {}
        if cur.get("run_id") != run_id:
            return
        cur = deepcopy(cur)
        cur["status"] = status
        cur["completed_at"] = _utc()
        cur["heartbeat_at"] = _utc()
        cur["last_heartbeat_at"] = cur["heartbeat_at"]
        cur["currently_processing"] = None
        cur["currently_processing_title"] = None
        cur["queued"] = max(0, int(cur.get("total_candidates") or 0) - int(cur.get("processed") or 0))
        if int(cur.get("processed") or 0) >= int(cur.get("total_candidates") or 0) and int(cur.get("total_candidates") or 0) > 0:
            cur["progress_percent"] = 100
        if error:
            cur["error_summary"] = error
        state["last_attempt"] = cur
        if status in {STATUS_COMPLETED, STATUS_COMPLETED_WITH_WARNINGS, STATUS_PAUSED_BUDGET}:
            state["last_successful_completion"] = cur
        state["current_run"] = None
        state["lock"] = {"held": False, "run_id": None, "since": None}
        state["next_scheduled_run"] = compute_next_scheduled_run()
        _save_state(state)


def research_status() -> dict[str, Any]:
    state = recover_stale_runs()
    cur = state.get("current_run")
    last_ok = state.get("last_successful_completion")
    last_attempt = state.get("last_attempt")
    running = bool(cur and cur.get("status") in {STATUS_RUNNING, STATUS_QUEUED})

    # Live backlog (authoritative pipeline)
    try:
        candidates = list_research_candidates()
        backlog = len(candidates)
    except Exception:
        candidates = []
        backlog = 0

    stalled = False
    if running and cur:
        hb = _parse(cur.get("heartbeat_at") or cur.get("last_heartbeat_at") or cur.get("started_at"))
        if hb and now_utc() - hb > timedelta(minutes=STALE_HEARTBEAT_MINUTES):
            stalled = True

    status_label = "IDLE"
    idle_reason = None
    if stalled:
        status_label = "STALLED"
    elif running:
        status_label = "RUNNING" if (cur or {}).get("status") == STATUS_RUNNING else "QUEUED"
    elif last_attempt and last_attempt.get("status") == STATUS_FAILED:
        la = _parse(last_attempt.get("completed_at"))
        lo = _parse((last_ok or {}).get("completed_at"))
        if la and (not lo or la > lo):
            status_label = "FAILED"
    elif backlog > 0:
        status_label = "BACKLOG"
        idle_reason = f"{backlog} opportunities awaiting research"
    elif last_ok:
        status_label = "CURRENT"
        idle_reason = "Research queue idle — no RESEARCH_QUEUED work remaining"
    else:
        status_label = "IDLE"
        idle_reason = "No research run yet" if research_enabled() else "Research runner disabled"

    progress = 0
    if running:
        progress = int((cur or {}).get("progress_percent") or 0)
    elif status_label == "CURRENT" and backlog == 0:
        progress = 100

    display = cur if running else (last_ok or last_attempt or {})
    return {
        "kind": "M3ResearchStatus",
        "enabled": research_enabled(),
        "interval_minutes": research_interval_minutes(),
        "status": status_label,
        "running": running,
        "stalled": stalled,
        "progress_percent": progress,
        "backlog": backlog,
        "idle_reason": idle_reason if not running else None,
        "elapsed_hint_started_at": (cur or {}).get("started_at") if running else None,
        "current_run": cur,
        "last_successful_completion": last_ok,
        "last_attempt": last_attempt,
        "next_scheduled_run": state.get("next_scheduled_run") or compute_next_scheduled_run(),
        "display": {
            "run_id": display.get("run_id"),
            "total_candidates": display.get("total_candidates"),
            "queued": display.get("queued") if running else backlog,
            "processed": display.get("processed"),
            "rejected": display.get("rejected"),
            "advanced": display.get("advanced"),
            "deferred": display.get("deferred"),
            "failed": display.get("failed"),
            "currently_processing": display.get("currently_processing") if running else None,
            "currently_processing_title": display.get("currently_processing_title") if running else None,
            "eta_label": (cur or {}).get("eta_label") if running else None,
            "eta_seconds": (cur or {}).get("eta_seconds") if running else None,
            "paid_actions_used": display.get("paid_actions_used"),
            "actual_external_spend": display.get("actual_external_spend"),
            "heartbeat_at": display.get("heartbeat_at") or display.get("last_heartbeat_at"),
            "started_at": display.get("started_at"),
            "completed_at": display.get("completed_at"),
        },
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
        "mode": mode_snapshot(),
        "commercial_outreach": False,
    }


def request_research_run(*, trigger_type: str = TRIGGER_MANUAL) -> dict[str, Any]:
    if not research_enabled() and trigger_type != TRIGGER_MANUAL:
        return {"accepted": False, "reason": "research_disabled", "status": research_status()}

    with _lock:
        state = recover_stale_runs()
        cur = state.get("current_run")
        if cur and cur.get("status") in {STATUS_RUNNING, STATUS_QUEUED}:
            return {
                "accepted": False,
                "already_running": True,
                "message": "Research already running",
                "run": cur,
                "status": research_status(),
            }

        candidates = list_research_candidates()
        if not candidates:
            return {
                "accepted": False,
                "reason": "no_candidates",
                "message": "No RESEARCH_QUEUED / QUEUE_RESEARCH opportunities to process",
                "status": research_status(),
            }

        run_id = f"MRR-{uuid4().hex[:12]}"
        total = min(len(candidates), research_max_per_run())
        run = _empty_run(run_id, trigger_type, total_candidates=total)
        baseline = _governor_snapshot()
        run["spend_baseline_absolute"] = baseline.get("absolute_used")
        run["spend_baseline_paid_actions"] = _paid_actions_count()
        state["current_run"] = run
        state["lock"] = {"held": True, "run_id": run_id, "since": _utc()}
        _save_state(state)

    _dispatch_research_job(run_id, trigger_type)
    return {"accepted": True, "already_running": False, "run_id": run_id, "status": research_status()}


def _dispatch_research_job(run_id: str, trigger_type: str) -> None:
    global _worker
    _worker = threading.Thread(
        target=_execute_run,
        args=(run_id, trigger_type),
        name=f"m3-research-{run_id}",
        daemon=True,
    )
    _worker.start()
    print(f"govtracker: research thread started {run_id} alive={_worker.is_alive()}", flush=True)
    log.info("Dispatched M3 research %s via thread (alive=%s)", run_id, _worker.is_alive())


def _mark_researched(store: Any, canonical_id: str, row: dict[str, Any]) -> None:
    row = store.get(canonical_id) or row
    row["research_completed_fingerprint"] = row.get("evidence_fingerprint")
    row["research_last_attempt_at"] = _utc()
    row["research_in_progress"] = False
    # Keep research_queued True only if still awaiting further automatic research
    action = _next_action_code(row)
    if action != NA_QUEUE_RESEARCH:
        row["research_queued"] = bool(action in {"AUTO_CONTINUE"})
    row["lifecycle"] = derive_lifecycle(row)
    row["pending_next_action"] = determine_next_action(row)
    store._rows[canonical_id] = row
    store.save()


def _execute_run(run_id: str, trigger_type: str) -> None:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    cost_gov = None
    try:
        from cost_governor import get_cost_governor

        cost_gov = get_cost_governor()
    except Exception:
        pass

    orch = M3EndToEndOrchestrator(store=store, cost_governor=cost_gov)
    warnings = False
    budget_paused = False

    try:
        candidates = list_research_candidates(store)[: research_max_per_run()]
        _update_run(run_id, total_candidates=len(candidates), queued=len(candidates))
        if not candidates:
            _finalize_run(run_id, status=STATUS_COMPLETED)
            return

        for row in candidates:
            cid = row["canonical_id"]
            title = str(row.get("title") or cid)[:120]
            _update_run(
                run_id,
                currently_processing=cid,
                currently_processing_title=title,
            )

            # Re-check fingerprint / need under lock of current store state
            live = store.get(cid) or row
            if not _needs_research(live):
                with _lock:
                    state = _load_state()
                    cur = state.get("current_run") or {}
                    if cur.get("run_id") == run_id:
                        cur["processed"] = int(cur.get("processed") or 0) + 1
                        cur["deferred"] = int(cur.get("deferred") or 0) + 1
                        ids = list(cur.get("processed_ids") or [])
                        ids.append(cid)
                        cur["processed_ids"] = ids[-500:]
                        state["current_run"] = cur
                        _save_state(state)
                _update_run(run_id)
                continue

            t0 = time.monotonic()
            error = None
            result = None
            try:
                # Budget gate before paid work
                if orch._budget_blocks_paid(estimated_cost=1.0):
                    outcome = "deferred"
                    warnings = True
                    budget_paused = True
                    error = None
                    result = {"stop_reason": "WAIT_BUDGET"}
                else:
                    result = orch.advance(cid, max_auto_steps=8)
                    after = store.get(cid)
                    outcome = _classify_outcome(live, after, error=None)
                    if after:
                        _mark_researched(store, cid, after)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)[:400]
                outcome = "failed"
                warnings = True
                log.exception("Research advance failed for %s", cid)
                try:
                    after = store.get(cid) or live
                    after["research_last_attempt_at"] = _utc()
                    after["research_in_progress"] = False
                    store._rows[cid] = after
                    store.save()
                except Exception:
                    pass

            elapsed = max(0.05, time.monotonic() - t0)
            snap = _governor_snapshot()
            with _lock:
                state = _load_state()
                cur = state.get("current_run") or {}
                if cur.get("run_id") != run_id:
                    return
                cur["processed"] = int(cur.get("processed") or 0) + 1
                cur[outcome] = int(cur.get(outcome) or 0) + 1
                samples = list(cur.get("throughput_seconds") or [])
                samples.append(elapsed)
                cur["throughput_seconds"] = samples[-50:]
                ids = list(cur.get("processed_ids") or [])
                ids.append(cid)
                cur["processed_ids"] = ids[-500:]
                baseline_abs = float(cur.get("spend_baseline_absolute") or 0)
                cur["actual_external_spend"] = round(
                    max(0.0, float(snap.get("absolute_used") or 0) - baseline_abs), 4
                )
                baseline_paid = int(cur.get("spend_baseline_paid_actions") or 0)
                cur["paid_actions_used"] = max(0, _paid_actions_count() - baseline_paid)
                if result and isinstance(result, dict) and result.get("stop_reason") == "WAIT_BUDGET":
                    cur["idle_reason"] = "cost_governor_blocked"
                state["current_run"] = cur
                _save_state(state)
            _update_run(run_id)

            if budget_paused:
                break

        final_status = STATUS_COMPLETED
        if budget_paused:
            final_status = STATUS_PAUSED_BUDGET
        elif warnings:
            final_status = STATUS_COMPLETED_WITH_WARNINGS
        _finalize_run(run_id, status=final_status)
        log.info("M3 research %s done status=%s", run_id, final_status)
    except Exception as exc:
        log.exception("M3 research run failed")
        _finalize_run(run_id, status=STATUS_FAILED, error=str(exc))


def maybe_startup_research() -> dict[str, Any]:
    if not research_enabled():
        return {"queued": False, "reason": "disabled"}
    state = clear_orphan_locks_on_boot()
    state["next_scheduled_run"] = compute_next_scheduled_run()
    _save_state(state)
    try:
        from m3_pipeline_store import M3PipelineStore
        from m3_discovery_service import restore_pipeline_store_from_db

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    candidates = list_research_candidates()
    if not candidates:
        print("govtracker: research backlog empty — skip startup research", flush=True)
        return {"queued": False, "reason": "no_candidates", "status": research_status()}
    print(
        f"govtracker: startup research backlog={len(candidates)} trigger={TRIGGER_STARTUP}",
        flush=True,
    )
    return request_research_run(trigger_type=TRIGGER_STARTUP)


def scheduled_research_tick() -> dict[str, Any]:
    if not research_enabled():
        return {"skipped": True, "reason": "disabled"}
    candidates = list_research_candidates()
    if not candidates:
        return {"skipped": True, "reason": "no_candidates", "status": research_status()}
    return request_research_run(trigger_type=TRIGGER_SCHEDULED)


def maybe_request_research_after_discovery() -> dict[str, Any]:
    """Called when discovery finishes — drain new RESEARCH_QUEUED without waiting for interval."""
    if not research_enabled():
        return {"queued": False, "reason": "disabled"}
    with _lock:
        state = recover_stale_runs()
        cur = state.get("current_run")
        if cur and cur.get("status") in {STATUS_RUNNING, STATUS_QUEUED}:
            return {"queued": False, "reason": "already_running", "status": research_status()}
    candidates = list_research_candidates()
    if not candidates:
        return {"queued": False, "reason": "no_candidates"}
    return request_research_run(trigger_type=TRIGGER_AFTER_DISCOVERY)
