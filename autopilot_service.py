"""Automatic contract processing — repair, attachments, and intake with no manual steps."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from api_budget import claude_intake_allowed, scheduled_sync_attachments_only

logger = logging.getLogger("govtracker.autopilot")

_lock = threading.Lock()
_running = False
_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None,
}

AutopilotTrigger = Literal["deploy", "post_sync", "nightly"]


def get_autopilot_status() -> dict[str, Any]:
    from workflow_backfill_service import get_repair_status

    with _lock:
        out = dict(_status)
    out["repair"] = get_repair_status()
    out["claude_intake_allowed"] = claude_intake_allowed()
    out["attachments_only_today"] = scheduled_sync_attachments_only()
    return out


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _status.update(kwargs)


def run_stored_pdf_repair_pass() -> dict[str, Any]:
    """Process every stored-PDF contract that still needs work. Errors skip to the next."""
    from workflow_backfill_service import repair_all_stored_attachment_contracts

    if not claude_intake_allowed():
        return {"skipped": True, "reason": "claude_intake_disabled"}

    _set_status(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        error=None,
    )
    try:
        stats = repair_all_stored_attachment_contracts()
        _set_status(last_result=stats)
        return stats
    finally:
        _set_status(running=False, finished_at=datetime.now(timezone.utc).isoformat())


def start_autopilot(*, trigger: AutopilotTrigger = "deploy") -> bool:
    """Background: pricing backfill, then repair every contract that still needs Claude."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        try:
            if trigger == "deploy" and not claude_intake_allowed():
                logger.info("Autopilot deploy: intake disabled — skipping Claude repair")
                return

            if trigger == "deploy":
                logger.info("Autopilot deploy: repair stored PDFs that still need work")
            elif trigger == "nightly":
                logger.info("Autopilot nightly: repair remaining stored PDFs after sync")
            else:
                logger.info("Autopilot post-sync: repair remaining stored PDFs")

            run_stored_pdf_repair_pass()
            from pricing_backfill_service import start_background_pricing_backfill

            start_background_pricing_backfill()
        except Exception:
            logger.exception("Autopilot thread failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-autopilot").start()
    return True


def run_scheduled_autopilot() -> None:
    """After 6am sync — repair anything still incomplete (errors skip to next contract)."""
    if not start_autopilot(trigger="nightly"):
        logger.info("Scheduled autopilot skipped — already running")
