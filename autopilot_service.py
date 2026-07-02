"""Automatic contract processing — repair, attachments, and intake with no manual steps."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

from api_budget import can_screen, can_spend_sam, intake_on_sync_enabled
from database import SessionLocal

logger = logging.getLogger("govtracker.autopilot")

_lock = threading.Lock()
_running = False
_claude_blocked_date: str | None = None
_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "round": 0,
    "last_cycle": None,
    "error": None,
    "claude_blocked_until": None,
}


def get_autopilot_status() -> dict[str, Any]:
    from workflow_backfill_service import get_repair_status

    _clear_claude_block_if_new_day()
    with _lock:
        out = dict(_status)
        out["claude_blocked_today"] = _claude_blocked_date == date.today().isoformat()
    out["repair"] = get_repair_status()
    return out


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _status.update(kwargs)


def _clear_claude_block_if_new_day() -> None:
    global _claude_blocked_date
    today = date.today().isoformat()
    if _claude_blocked_date and _claude_blocked_date < today:
        _claude_blocked_date = None
        _set_status(claude_blocked_until=None)


def _block_claude_for_today(reason: str) -> None:
    global _claude_blocked_date
    _claude_blocked_date = date.today().isoformat()
    _set_status(claude_blocked_until="tomorrow_6am_sync")
    logger.info("Autopilot: Claude paused for today (%s) — will resume after next daily sync", reason)


def claude_work_allowed() -> bool:
    """False after Claude credits/budget are exhausted until the next calendar day."""
    _clear_claude_block_if_new_day()
    return _claude_blocked_date is None


def has_autopilot_work() -> bool:
    """True when SAM pulls or Claude intake/repair remain."""
    from attachment_storage import has_stored_pdfs
    from screening_pipeline import has_attachments_ready
    from sync import list_attachment_backlog
    from workflow_backfill_service import contract_repair_reason, contracts_needing_repair

    session = SessionLocal()
    try:
        for row in contracts_needing_repair(session):
            if has_stored_pdfs(session, row.id) and contract_repair_reason(row, session):
                return True

        if can_spend_sam(1):
            backlog = list_attachment_backlog(session)
            if any(not has_attachments_ready(r, session) for r in backlog):
                return True

        if claude_work_allowed() and intake_on_sync_enabled() and can_screen():
            for row in contracts_needing_repair(session):
                if has_stored_pdfs(session, row.id):
                    return True
    finally:
        session.close()
    return False


def autopilot_cycle(*, batch_size: int = 2, full_stored_repair: bool = False) -> dict[str, Any]:
    """One pass: repair stored PDFs → SAM attachment pulls → repair batch → intake."""
    from intake import enrich_matching_attachments, intake_pending
    from workflow_backfill_service import repair_all_stored_attachment_contracts, run_workflow_repair_batch

    cycle: dict[str, Any] = {
        "stored_repaired": 0,
        "enriched": 0,
        "batch_repaired": 0,
        "intake_processed": 0,
        "halt_reason": None,
        "work_units": 0,
        "claude_skipped": not claude_work_allowed(),
    }

    if claude_work_allowed():
        if full_stored_repair:
            stored = repair_all_stored_attachment_contracts()
            cycle["stored_repaired"] = stored.get("repaired", 0)
            cycle["work_units"] += stored.get("processed", 0)
            if stored.get("halt_reason") in ("claude_api", "screen_budget"):
                cycle["halt_reason"] = stored["halt_reason"]
                _block_claude_for_today(stored["halt_reason"])
                return cycle
        else:
            batch = run_workflow_repair_batch(limit=batch_size)
            cycle["batch_repaired"] = batch.get("repaired", 0)
            cycle["work_units"] += batch.get("processed", 0)
            if batch.get("halt_reason") in ("claude_api", "screen_budget"):
                cycle["halt_reason"] = batch["halt_reason"]
                _block_claude_for_today(batch["halt_reason"])
                return cycle

    if can_spend_sam(1):
        session = SessionLocal()
        try:
            enrich = enrich_matching_attachments(session, limit=batch_size)
            cycle["enriched"] = enrich.get("attachments_enriched", 0)
            cycle["work_units"] += cycle["enriched"]
            if any("SAM.gov daily budget" in e for e in enrich.get("errors", [])):
                cycle["halt_reason"] = "sam_budget"
        finally:
            session.close()

    if claude_work_allowed() and intake_on_sync_enabled() and can_screen():
        intake = intake_pending(limit=batch_size, matching_only=True)
        cycle["intake_processed"] = intake.get("processed", 0)
        cycle["work_units"] += cycle["intake_processed"]
        if any("Claude budget" in e for e in intake.get("errors", [])):
            cycle["halt_reason"] = "screen_budget"
            _block_claude_for_today("screen_budget")

    return cycle


def run_autopilot_until_idle(*, max_rounds: int = 10, batch_size: int = 2) -> dict[str, Any]:
    """Process backlog in a few tight rounds, then stop. No periodic retries."""
    totals: dict[str, Any] = {"rounds": 0, "halt_reason": None}
    _set_status(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        error=None,
        round=0,
        last_cycle=None,
    )
    try:
        for round_num in range(1, max_rounds + 1):
            cycle = autopilot_cycle(
                batch_size=batch_size,
                full_stored_repair=(round_num == 1),
            )
            totals["rounds"] = round_num
            totals["last_cycle"] = cycle
            _set_status(round=round_num, last_cycle=cycle)

            halt = cycle.get("halt_reason")
            if halt in ("claude_api", "screen_budget"):
                totals["halt_reason"] = halt
                break
            if cycle.get("work_units", 0) == 0:
                break
            time.sleep(1)

        logger.info("Autopilot idle after %s round(s), halt=%s", totals["rounds"], totals.get("halt_reason"))
        return totals
    except Exception as exc:
        _set_status(error=str(exc)[:500])
        logger.exception("Autopilot failed")
        raise
    finally:
        _set_status(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def start_autopilot(*, batch_size: int = 2) -> bool:
    """Start the pipeline once (deploy or post-sync). Returns immediately."""
    global _running
    _clear_claude_block_if_new_day()
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        try:
            from pricing_backfill_service import is_pricing_backfill_complete, run_one_time_pricing_backfill

            if not is_pricing_backfill_complete():
                logger.info("Autopilot: running one-time pricing backfill (no Claude)")
                run_one_time_pricing_backfill()

            if not has_autopilot_work():
                logger.info("Autopilot: nothing to do — skipping")
                return

            logger.info("Autopilot: starting repair + intake pass")
            run_autopilot_until_idle(batch_size=batch_size)
        except Exception:
            logger.exception("Autopilot thread failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-autopilot").start()
    return True


def run_scheduled_autopilot() -> None:
    """Called after the 6am daily SAM sync — not on a timer."""
    _clear_claude_block_if_new_day()
    if not has_autopilot_work():
        logger.info("Scheduled autopilot: backlog empty — skipping")
        return
    if not start_autopilot():
        logger.info("Scheduled autopilot skipped — already running")
