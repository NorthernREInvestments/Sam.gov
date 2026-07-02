"""One-time pricing backfill on app startup — USAspending + stored PDFs only, no SAM.gov."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from database import SessionLocal
from models import Contract
from prior_contract_extract import backfill_prior_contract_and_pricing
from settings_store import is_pricing_backfill_complete, mark_pricing_backfill_complete

logger = logging.getLogger("govtracker.pricing_backfill")
_lock = threading.Lock()
_running = False


def run_one_time_pricing_backfill() -> dict[str, int]:
    """
    Refresh prior-contract hints + USAspending pricing for every contract once per deploy.
    Skips if already completed (flag in app_settings).
    """
    if is_pricing_backfill_complete():
        logger.info("One-time pricing backfill already completed — skipping")
        return {"skipped": 1}

    session = SessionLocal()
    stats = {
        "processed": 0,
        "prior_found": 0,
        "hints_in_pdf": 0,
        "errors": 0,
    }
    try:
        rows = session.query(Contract).order_by(Contract.id).all()
        logger.info("Starting one-time pricing backfill for %s contract(s) — no SAM.gov calls", len(rows))

        for row in rows:
            try:
                result = backfill_prior_contract_and_pricing(session, row)
                session.commit()
                stats["processed"] += 1
                if result.get("previous_contract_number") or result.get("incumbent_contractor"):
                    stats["hints_in_pdf"] += 1
                if result.get("is_prior_contract"):
                    stats["prior_found"] += 1
            except Exception:
                session.rollback()
                stats["errors"] += 1
                logger.exception("Pricing backfill failed for %s", row.notice_id)

        mark_pricing_backfill_complete(session)
        session.commit()
        logger.info(
            "One-time pricing backfill done: %s processed, %s prior matches, %s errors",
            stats["processed"],
            stats["prior_found"],
            stats["errors"],
        )
    finally:
        session.close()
    return stats


def start_background_pricing_backfill() -> None:
    """Run one-time pricing backfill in a daemon thread so startup is not blocked."""
    global _running
    with _lock:
        if _running or is_pricing_backfill_complete():
            return
        _running = True

    def _run() -> None:
        global _running
        try:
            run_one_time_pricing_backfill()
        except Exception:
            logger.exception("One-time pricing backfill failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-pricing-backfill").start()
