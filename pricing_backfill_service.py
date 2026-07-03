"""Pricing backfill on deploy and after sync — USAspending + stored PDFs only, no SAM.gov."""

from __future__ import annotations

import logging
import threading
from typing import Any

from database import SessionLocal, with_db_retry
from models import Contract
from pricing import contract_missing_prior_dollars
from prior_contract_extract import backfill_prior_contract_and_pricing

logger = logging.getLogger("govtracker.pricing_backfill")
_lock = threading.Lock()
_running = False
_last_result: dict[str, int] | None = None


def _scored_contract_ids() -> list[int]:
    """IDs for contracts with a score — light query, no attachment_text."""
    session = SessionLocal()
    try:
        rows = session.query(Contract.id, Contract.analysis).order_by(Contract.due_date).all()
        ids: list[int] = []
        for cid, analysis in rows:
            a = analysis if isinstance(analysis, dict) else {}
            if a.get("score") is not None or a.get("text_score") is not None:
                ids.append(cid)
        return ids
    finally:
        session.close()


def _backfill_contract_pricing(contract_id: int) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        session = SessionLocal()
        try:
            row = session.query(Contract).filter(Contract.id == contract_id).first()
            if not row:
                return {"skipped": True, "reason": "not_found"}
            if not contract_missing_prior_dollars(row):
                return {"skipped": True, "reason": "has_dollars"}
            result = backfill_prior_contract_and_pricing(session, row)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return with_db_retry(work)


def run_missing_dollar_backfill() -> dict[str, int]:
    """Re-query USAspending for scored contracts that still show no prior-contract dollars."""
    ids = _scored_contract_ids()
    stats = {"processed": 0, "found": 0, "errors": 0, "targets": 0, "skipped": 0}

    logger.info("Missing-dollar pricing refresh for %s scored contract(s)", len(ids))

    for contract_id in ids:
        session = SessionLocal()
        try:
            row = session.query(Contract).filter(Contract.id == contract_id).first()
            if not row or not contract_missing_prior_dollars(row):
                continue
            notice_id = row.notice_id
        finally:
            session.close()

        stats["targets"] += 1
        try:
            result = _backfill_contract_pricing(contract_id)
            if result.get("skipped"):
                stats["skipped"] += 1
                continue
            stats["processed"] += 1
            if result.get("is_prior_contract"):
                stats["found"] += 1
                logger.info("Pricing found for %s", notice_id)
        except Exception:
            stats["errors"] += 1
            logger.exception("Missing-dollar pricing refresh failed for %s", notice_id)

    logger.info(
        "Missing-dollar pricing refresh done: %s found, %s errors, %s skipped (of %s targets)",
        stats["found"],
        stats["errors"],
        stats["skipped"],
        stats["targets"],
    )
    return stats


def get_pricing_backfill_status() -> dict[str, Any]:
    with _lock:
        return {
            "running": _running,
            "last_result": _last_result,
        }


def start_background_pricing_backfill() -> dict[str, Any]:
    """Run missing-dollar USAspending refresh in a daemon thread."""
    global _running, _last_result
    with _lock:
        if _running:
            return {"started": False, "reason": "already_running", "status": get_pricing_backfill_status()}
        _running = True

    def _run() -> None:
        global _running, _last_result
        try:
            _last_result = run_missing_dollar_backfill()
        except Exception:
            logger.exception("Pricing backfill failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-pricing-backfill").start()
    logger.info("Background pricing backfill started")
    return {"started": True, "status": get_pricing_backfill_status()}
