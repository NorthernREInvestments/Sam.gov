"""Automatic contract processing — repair, attachments, and intake with no manual steps."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from api_budget import can_screen, can_spend_sam, intake_on_sync_enabled
from database import SessionLocal

logger = logging.getLogger("govtracker.autopilot")

_lock = threading.Lock()
_running = False
_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "round": 0,
    "last_cycle": None,
    "error": None,
}


def get_autopilot_status() -> dict[str, Any]:
    from workflow_backfill_service import get_repair_status

    with _lock:
        out = dict(_status)
    out["repair"] = get_repair_status()
    return out


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _status.update(kwargs)


def autopilot_cycle(*, batch_size: int = 3) -> dict[str, Any]:
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
    }

    stored = repair_all_stored_attachment_contracts()
    cycle["stored_repaired"] = stored.get("repaired", 0)
    cycle["work_units"] += stored.get("processed", 0)
    if stored.get("halt_reason") in ("claude_api", "screen_budget"):
        cycle["halt_reason"] = stored["halt_reason"]
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

    batch = run_workflow_repair_batch(limit=batch_size)
    cycle["batch_repaired"] = batch.get("repaired", 0)
    cycle["work_units"] += batch.get("processed", 0)
    if batch.get("halt_reason") in ("claude_api", "screen_budget"):
        cycle["halt_reason"] = batch["halt_reason"]
        return cycle

    if intake_on_sync_enabled() and can_screen():
        intake = intake_pending(limit=batch_size, matching_only=True)
        cycle["intake_processed"] = intake.get("processed", 0)
        cycle["work_units"] += cycle["intake_processed"]
        if any("Claude budget" in e for e in intake.get("errors", [])):
            cycle["halt_reason"] = "screen_budget"

    return cycle


def run_autopilot_until_idle(*, max_rounds: int = 120, batch_size: int = 3) -> dict[str, Any]:
    """Keep processing until the queue is empty or API budgets block progress."""
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
            cycle = autopilot_cycle(batch_size=batch_size)
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


def start_autopilot(*, batch_size: int = 3) -> bool:
    """Start the full automatic pipeline on deploy (returns immediately)."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        try:
            from pricing_backfill_service import is_pricing_backfill_complete, run_one_time_pricing_backfill

            if not is_pricing_backfill_complete():
                logger.info("Autopilot: running one-time pricing backfill")
                run_one_time_pricing_backfill()

            logger.info("Autopilot: starting automatic repair + intake")
            run_autopilot_until_idle(batch_size=batch_size)
        except Exception:
            logger.exception("Autopilot thread failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-autopilot").start()
    return True


def run_scheduled_autopilot() -> None:
    """Scheduler hook — continue backlog processing if autopilot is not already running."""
    if not start_autopilot():
        logger.info("Scheduled autopilot skipped — already running")
