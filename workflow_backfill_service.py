"""Automatically repair every contract through the correct intake pipeline — no manual force."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from api_budget import ScreenBudgetExceeded, can_screen
from database import SessionLocal
from models import Contract
from screening_pipeline import (
    WORKFLOW_VERSION,
    has_attachments_ready,
    needs_intake,
    pdfs_expected_on_contract,
    workflow_is_current,
)

logger = logging.getLogger("govtracker.workflow_backfill")
_lock = threading.Lock()
_running = False


def _is_anthropic_credits_exhausted(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "credit balance" in msg and "anthropic" in msg


def contract_repair_reason(row: Contract) -> str | None:
    """Why this contract still needs automated repair, or None if current."""
    analysis = row.analysis if isinstance(row.analysis, dict) else {}

    if not workflow_is_current(analysis):
        return "stale_workflow"

    if not has_attachments_ready(row):
        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if raw and (pdfs_expected_on_contract(row) or raw.get("descriptionText")):
            return "pending_attachments"
        return None

    if needs_intake(row):
        return "needs_intake"

    if getattr(row, "sub_search_status", None) not in ("complete", "error"):
        return "pending_sub_search"

    if analysis.get("screening_stage") == "full" and not analysis.get("contract_advice"):
        return "missing_advice"

    return None


def repair_contract(session, row: Contract) -> dict[str, Any]:
    """
    Run the full correct order for one contract using only stored database data:
    attachments (from PostgreSQL PDF bytes) → prior pricing hints → PDF sub type → find subs → Claude rank.
    Never calls SAM.gov.
    """
    from attachment_pipeline import ensure_attachments_from_database
    from intake import full_intake_contract
    from prior_contract_extract import merge_prior_contract_hints, refresh_pricing_after_pdf_extract

    reason = contract_repair_reason(row)
    if not reason:
        return {"notice_id": row.notice_id, "skipped": True, "reason": "current"}

    merge_prior_contract_hints(row)
    session.flush()

    if not has_attachments_ready(row):
        try:
            ensure_attachments_from_database(session, row)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("DB attachment extraction failed for %s", row.notice_id)
            return {"notice_id": row.notice_id, "error": "attachment_extract_failed"}

        if not has_attachments_ready(row):
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "pending_attachments",
                "repair_reason": reason,
                "message": "No PDF bytes or attachment text stored in the database for this contract.",
            }

    try:
        refresh_pricing_after_pdf_extract(row)
    except Exception:
        pass

    if not can_screen():
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "repair_reason": reason,
        }

    try:
        result = full_intake_contract(row, session=session, force=True, db_only=True)
        session.commit()
        result["repair_reason"] = reason
        return result
    except ScreenBudgetExceeded:
        session.rollback()
        return {
            "notice_id": row.notice_id,
            "skipped": True,
            "reason": "screen_budget",
            "repair_reason": reason,
        }
    except Exception as exc:
        session.rollback()
        if _is_anthropic_credits_exhausted(exc):
            logger.error("Workflow repair halted — Anthropic API credits exhausted")
            return {
                "notice_id": row.notice_id,
                "skipped": True,
                "reason": "claude_credits",
                "repair_reason": reason,
                "detail": str(exc)[:200],
            }
        logger.exception("Workflow repair failed for %s", row.notice_id)
        return {
            "notice_id": row.notice_id,
            "error": "intake_failed",
            "repair_reason": reason,
            "detail": str(exc)[:200],
        }


def contracts_needing_repair(session) -> list[Contract]:
    """Matching contracts that still need the v2 workflow — no score filter."""
    from datetime import date

    from sam_client import min_days_from_env, naics_from_env

    naics_codes = naics_from_env()
    if not naics_codes:
        return []

    min_days = min_days_from_env()
    today = date.today()
    rows = session.query(Contract).filter(Contract.naics_code.in_(naics_codes)).all()
    out: list[Contract] = []
    for row in rows:
        if row.due_date is not None and (row.due_date - today).days < min_days:
            continue
        if contract_repair_reason(row):
            out.append(row)
    out.sort(key=lambda r: (r.due_date is None, r.due_date or date.max, r.id or 0))
    return out


def run_workflow_repair_batch(*, limit: int = 5) -> dict[str, Any]:
    """Repair up to `limit` contracts in correct pipeline order."""
    session = SessionLocal()
    stats: dict[str, Any] = {
        "processed": 0,
        "repaired": 0,
        "attachments_pending": 0,
        "skipped": 0,
        "errors": 0,
        "halt_reason": None,
        "workflow_version": WORKFLOW_VERSION,
    }
    try:
        candidates = contracts_needing_repair(session)
        stats["queue_size"] = len(candidates)

        for row in candidates[:limit]:
            if not can_screen() and has_attachments_ready(row):
                stats["halt_reason"] = "screen_budget"
                break

            row_session = SessionLocal()
            try:
                result = repair_contract(row_session, row)
            finally:
                row_session.close()

            stats["processed"] += 1

            if result.get("reason") == "claude_credits":
                stats["halt_reason"] = "claude_credits"
                break
            if result.get("error"):
                stats["errors"] += 1
            elif result.get("reason") == "screen_budget":
                stats["halt_reason"] = "screen_budget"
                break
            elif result.get("reason") == "pending_attachments":
                stats["attachments_pending"] += 1
            elif result.get("skipped") and result.get("reason") == "current":
                stats["skipped"] += 1
            elif result.get("screened") or result.get("full_analysis") or result.get("scope_extracted"):
                stats["repaired"] += 1
            elif not result.get("skipped"):
                stats["repaired"] += 1

        stats["remaining"] = len(contracts_needing_repair(session))
    finally:
        session.close()
    return stats


def run_workflow_repair_until_idle(*, batch_size: int = 5, max_rounds: int = 200) -> dict[str, Any]:
    """Keep repairing until the queue is empty or API budgets block progress."""
    totals: dict[str, Any] = {
        "rounds": 0,
        "processed": 0,
        "repaired": 0,
        "attachments_pending": 0,
        "errors": 0,
        "halt_reason": None,
        "workflow_version": WORKFLOW_VERSION,
    }
    for _ in range(max_rounds):
        batch = run_workflow_repair_batch(limit=batch_size)
        totals["rounds"] += 1
        totals["processed"] += batch.get("processed", 0)
        totals["repaired"] += batch.get("repaired", 0)
        totals["attachments_pending"] += batch.get("attachments_pending", 0)
        totals["errors"] += batch.get("errors", 0)

        remaining = batch.get("remaining", 0)
        halt = batch.get("halt_reason")
        if halt:
            totals["halt_reason"] = halt
            break
        if batch.get("processed", 0) == 0 or remaining == 0:
            break
        time.sleep(0.5)

    totals["remaining"] = 0
    session = SessionLocal()
    try:
        totals["remaining"] = len(contracts_needing_repair(session))
    finally:
        session.close()
    logger.info(
        "Workflow repair idle: %s repaired, %s remaining, halt=%s",
        totals["repaired"],
        totals["remaining"],
        totals["halt_reason"],
    )
    return totals


def start_background_workflow_repair(*, batch_size: int = 5) -> None:
    """
    On deploy: repair all stale contracts automatically, then hand off to normal intake.
    Replaces one-off manual Force Full Analysis / Find Subs for backlog contracts.
    """
    global _running
    with _lock:
        if _running:
            return
        _running = True

    def _run() -> None:
        global _running
        try:
            logger.info("Starting automatic workflow repair (target workflow v%s)", WORKFLOW_VERSION)
            totals = run_workflow_repair_until_idle(batch_size=batch_size)
            logger.info("Workflow repair pass finished: %s", totals)

            from intake import start_background_intake

            if totals.get("remaining", 0) > 0 or totals.get("halt_reason"):
                run_workflow_repair_until_idle(batch_size=batch_size)

            start_background_intake(batch_size=batch_size)
        except Exception:
            logger.exception("Background workflow repair failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, daemon=True, name="govtracker-workflow-repair").start()
