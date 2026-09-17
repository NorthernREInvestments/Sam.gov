"""Delete past-due opportunities that were never pursued."""

from __future__ import annotations
from application_clock import now_utc, today_local

import logging
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from csv_import_service import _is_protected_status, remove_expired_csv_rows
from models import Contract, CsvOpportunity, Proposal

logger = logging.getLogger("govtracker.expired_purge")

# User actively worked these — keep even after the response deadline.
PURSUED_CONTRACT_STATUSES = frozenset(
    {
        "bidding",
        "submitted",
        "won",
        "lost",
        "awarded",
        "active",
        "option_year",
        "stop_work",
        "completed",
        "not_awarded",
    }
)


def contract_was_pursued(session: Session, contract: Contract) -> bool:
    """True when the user went after this opportunity (not just AI recommend)."""
    status = (contract.status or "").strip().lower()
    if status in PURSUED_CONTRACT_STATUSES:
        return True
    if contract.selected_sub_quote is not None:
        return True
    if contract.award_date is not None:
        return True
    if contract.period_of_performance_start is not None:
        return True

    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    csv_meta = analysis.get("csv_import") if isinstance(analysis.get("csv_import"), dict) else {}
    if csv_meta.get("pursued_manually"):
        return True

    csv_row = session.query(CsvOpportunity).filter_by(notice_id=contract.notice_id).first()
    if csv_row and _is_protected_status(csv_row.status):
        return True

    has_proposal = (
        session.query(Proposal.id).filter(Proposal.contract_id == contract.id).limit(1).first()
        is not None
    )
    return has_proposal


def remove_expired_unpursued_contracts(session: Session, *, today: date | None = None) -> int:
    """Delete dashboard contracts past due_date that were never pursued."""
    today = today or today_local()
    candidates = (
        session.query(Contract)
        .filter(Contract.due_date.isnot(None))
        .filter(Contract.due_date < today)
        .all()
    )
    deleted = 0
    for row in candidates:
        if contract_was_pursued(session, row):
            continue
        session.delete(row)
        deleted += 1
    if deleted:
        session.flush()
        logger.info("Removed %s expired unpursued contract(s)", deleted)
    return deleted


def purge_expired_unpursued(session: Session, *, today: date | None = None) -> dict[str, Any]:
    """
    Remove past-due CSV rows and contracts that were not pursued.

    Keeps: Pursuing / Submitted / Won / Lost (CSV), bidding+ / proposals / awards (contracts).
    """
    today = today or today_local()
    contracts_removed = remove_expired_unpursued_contracts(session, today=today)
    csv_removed = remove_expired_csv_rows(session, today=today)

    queue_cleared = 0
    if contracts_removed or csv_removed:
        from csv_attachment_queue_service import clear_pending_queue_for_deleted_csv

        queue_cleared = clear_pending_queue_for_deleted_csv(session) or 0

    return {
        "contracts_removed": contracts_removed,
        "csv_removed": csv_removed,
        "queue_cleared": queue_cleared,
    }
