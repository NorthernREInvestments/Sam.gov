"""Automatic contract processing — repair, attachments, and intake with no manual steps."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Any, Literal

from api_budget import (
    ClaudePipelineHalt,
    can_screen,
    can_spend_sam,
    claude_intake_allowed,
    scheduled_sync_attachments_only,
)
from database import SessionLocal

logger = logging.getLogger("govtracker.autopilot")

_lock = threading.Lock()
_running = False
_claude_halted_today: str | None = None
_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_cycle": None,
    "error": None,
    "claude_halt_reason": None,
}

AutopilotTrigger = Literal["deploy", "post_sync"]


def get_autopilot_status() -> dict[str, Any]:
    from workflow_backfill_service import get_repair_status

    _clear_claude_halt_if_new_day()
    with _lock:
        out = dict(_status)
        out["claude_halted_today"] = _claude_halted_today == date.today().isoformat()
    out["repair"] = get_repair_status()
    out["claude_intake_allowed"] = claude_intake_allowed()
    out["attachments_only_today"] = scheduled_sync_attachments_only()
    return out


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _status.update(kwargs)


def _clear_claude_halt_if_new_day() -> None:
    global _claude_halted_today
    today = date.today().isoformat()
    if _claude_halted_today and _claude_halted_today < today:
        _claude_halted_today = None
        _set_status(claude_halt_reason=None)


def record_claude_halt(reason: str) -> None:
    """Stop all automatic Claude work for the rest of the day — no retry loops."""
    global _claude_halted_today
    _claude_halted_today = date.today().isoformat()
    _set_status(claude_halt_reason=reason)
    logger.error("Claude pipeline halted for today (%s) — no further automatic calls", reason)


def claude_work_allowed() -> bool:
    _clear_claude_halt_if_new_day()
    if _claude_halted_today is not None:
        return False
    return claude_intake_allowed()


def run_deploy_repair_pass() -> dict[str, Any]:
    """One-shot repair of stored-PDF contracts. Stops on first Claude API failure."""
    from workflow_backfill_service import repair_all_stored_attachment_contracts

    if not claude_work_allowed():
        return {"skipped": True, "reason": "claude_halted_or_disabled"}

    _set_status(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        error=None,
    )
    try:
        stats = repair_all_stored_attachment_contracts()
        halt = stats.get("halt_reason")
        if halt in ("claude_api", "screen_budget"):
            record_claude_halt(halt)
        return stats
    except ClaudePipelineHalt as exc:
        record_claude_halt(exc.reason)
        return {"halt_reason": exc.reason, "notice_id": exc.notice_id}
    finally:
        _set_status(running=False, finished_at=datetime.now(timezone.utc).isoformat())


def start_autopilot(*, trigger: AutopilotTrigger = "deploy") -> bool:
    """Background: pricing backfill + one repair pass. No retry loops."""
    global _running
    _clear_claude_halt_if_new_day()
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        try:
            from pricing_backfill_service import is_pricing_backfill_complete, run_one_time_pricing_backfill

            if not is_pricing_backfill_complete():
                logger.info("Autopilot: pricing backfill (no Claude)")
                run_one_time_pricing_backfill()

            if trigger == "deploy":
                if claude_work_allowed():
                    logger.info("Autopilot deploy: one-shot Claude repair (stored PDFs)")
                    run_deploy_repair_pass()
                else:
                    logger.info("Autopilot deploy: Claude halted or disabled — skipping repair")
                return

            if scheduled_sync_attachments_only():
                logger.info("Autopilot post-sync: skipped (attachments-only sync handles SAM+Claude)")
                return

            if claude_work_allowed():
                logger.info("Autopilot post-sync: one-shot repair pass")
                run_deploy_repair_pass()
        except Exception:
            logger.exception("Autopilot thread failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-autopilot").start()
    return True


def run_scheduled_autopilot() -> None:
    """After 6am sync — one repair pass only if Claude still allowed."""
    if scheduled_sync_attachments_only():
        return
    if not start_autopilot(trigger="post_sync"):
        logger.info("Scheduled autopilot skipped — already running")
