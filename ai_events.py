"""Event-driven AI foundation — creates pending jobs, never executes AI."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from ai_operating_modes import get_operating_mode

EVENT_SOLICITATION_PACKAGE_COMPLETE = "SOLICITATION_PACKAGE_COMPLETE"
EVENT_AMENDMENT_RECEIVED = "AMENDMENT_RECEIVED"
EVENT_SUPPLIER_TRANSCRIPT_ADDED = "SUPPLIER_TRANSCRIPT_ADDED"
EVENT_FINANCIER_TRANSCRIPT_ADDED = "FINANCIER_TRANSCRIPT_ADDED"
EVENT_CO_TRANSCRIPT_ADDED = "CO_TRANSCRIPT_ADDED"
EVENT_SUPPLIER_QUOTE_ADDED = "SUPPLIER_QUOTE_ADDED"
EVENT_FINANCING_TERMS_ADDED = "FINANCING_TERMS_ADDED"
EVENT_FUNDING_PLAN_CHANGED = "FUNDING_PLAN_CHANGED"
EVENT_ASK_ABOUT_DEAL = "ASK_ABOUT_DEAL"
EVENT_FINAL_BID_REVIEW_REQUESTED = "FINAL_BID_REVIEW_REQUESTED"

JOB_PENDING = "PENDING"
JOB_SKIPPED = "SKIPPED"
JOB_COMPLETE = "COMPLETE"


def emit_ai_event(
    session: Any,
    *,
    trigger_event: str,
    contract_id: int | None = None,
    payload: dict[str, Any] | None = None,
    skip_duplicate: bool = True,
) -> dict[str, Any]:
    """Record pending AI analysis job — router executes later."""
    from models import AiAnalysisJob

    if skip_duplicate and contract_id is not None:
        existing = (
            session.query(AiAnalysisJob)
            .filter_by(contract_id=contract_id, trigger_event=trigger_event, status=JOB_PENDING)
            .first()
        )
        if existing:
            return {
                "job_id": existing.id,
                "status": JOB_SKIPPED,
                "reason": "duplicate_pending_job",
                "LIVE_API_REQUESTS": 0,
                "OpenAI": 0,
            }

    mode = get_operating_mode()
    if trigger_event.endswith("_TRANSCRIPT_ADDED") and mode == "LEAN":
        auto = False
    else:
        auto = mode != "LEAN"

    row = AiAnalysisJob(
        contract_id=contract_id,
        trigger_event=trigger_event,
        payload_json=dict(payload or {}),
        status=JOB_PENDING,
        operating_mode=mode,
    )
    session.add(row)
    session.flush()
    return {
        "job_id": row.id,
        "trigger_event": trigger_event,
        "status": JOB_PENDING,
        "operating_mode": mode,
        "will_auto_execute_later": auto,
        "executed_at": None,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def mark_job_complete(session: Any, job_id: int) -> None:
    from models import AiAnalysisJob

    row = session.query(AiAnalysisJob).filter_by(id=job_id).first()
    if row:
        row.status = JOB_COMPLETE
        row.executed_at = now_utc()
