"""One-time pricing backfill on app startup — USAspending + stored PDFs only, no SAM.gov."""

from __future__ import annotations

import logging
import threading

from database import SessionLocal
from models import Contract
from pricing import contract_pricing_needs_refresh
from prior_contract_extract import backfill_prior_contract_and_pricing
from settings_store import (
    is_exact_match_fix_complete,
    is_pricing_agency_fix_complete,
    is_pricing_backfill_complete,
    mark_exact_match_fix_complete,
    mark_pricing_agency_fix_complete,
    mark_pricing_backfill_complete,
)

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


def run_pricing_agency_fix_repair() -> dict[str, int]:
    """Re-fetch USAspending pricing when agency filter left rows with contract # but no dollars."""
    if is_pricing_agency_fix_complete():
        logger.info("Pricing agency-filter repair already completed — skipping")
        return {"skipped": 1}

    session = SessionLocal()
    stats = {"processed": 0, "refreshed": 0, "errors": 0}
    try:
        rows = session.query(Contract).order_by(Contract.id).all()
        targets = [row for row in rows if contract_pricing_needs_refresh(row)]
        logger.info(
            "Starting pricing agency-filter repair for %s/%s contract(s)",
            len(targets),
            len(rows),
        )
        for row in targets:
            try:
                result = backfill_prior_contract_and_pricing(session, row)
                session.commit()
                stats["processed"] += 1
                if result.get("is_prior_contract") or result.get("annual_amount"):
                    stats["refreshed"] += 1
            except Exception:
                session.rollback()
                stats["errors"] += 1
                logger.exception("Pricing agency repair failed for %s", row.notice_id)

        mark_pricing_agency_fix_complete(session)
        session.commit()
        logger.info(
            "Pricing agency-filter repair done: %s refreshed, %s errors",
            stats["refreshed"],
            stats["errors"],
        )
    finally:
        session.close()
    return stats


def run_exact_match_fix_repair() -> dict[str, int]:
    """Re-run pricing with expanded exact-match lookup (contract # variants, site profiles)."""
    if is_exact_match_fix_complete():
        logger.info("Exact-match pricing repair already completed — skipping")
        return {"skipped": 1}

    session = SessionLocal()
    stats = {"processed": 0, "exact_found": 0, "errors": 0}
    try:
        rows = session.query(Contract).order_by(Contract.id).all()
        logger.info("Starting exact-match pricing repair for %s contract(s)", len(rows))
        for row in rows:
            try:
                result = backfill_prior_contract_and_pricing(session, row)
                session.commit()
                stats["processed"] += 1
                pred = (result.get("pricing_intel") or {}).get("predecessor_award") if isinstance(result.get("pricing_intel"), dict) else {}
                if isinstance(pred, dict) and pred.get("is_prior_contract"):
                    stats["exact_found"] += 1
            except Exception:
                session.rollback()
                stats["errors"] += 1
                logger.exception("Exact-match repair failed for %s", row.notice_id)

        mark_exact_match_fix_complete(session)
        session.commit()
        logger.info(
            "Exact-match pricing repair done: %s exact, %s errors",
            stats["exact_found"],
            stats["errors"],
        )
    finally:
        session.close()
    return stats


def start_background_pricing_backfill() -> None:
    """Run pricing backfill/repair in a daemon thread so startup is not blocked."""
    global _running
    with _lock:
        if _running:
            return
        if (
            is_pricing_backfill_complete()
            and is_pricing_agency_fix_complete()
            and is_exact_match_fix_complete()
        ):
            return
        _running = True

    def _run() -> None:
        global _running
        try:
            if not is_pricing_backfill_complete():
                run_one_time_pricing_backfill()
            if not is_pricing_agency_fix_complete():
                run_pricing_agency_fix_repair()
            if not is_exact_match_fix_complete():
                run_exact_match_fix_repair()
        except Exception:
            logger.exception("Pricing backfill/repair failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-pricing-backfill").start()
