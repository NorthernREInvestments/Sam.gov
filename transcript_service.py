"""Call transcript intake + analysis contract — manual-first, no OpenAI."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from ai_events import (
    EVENT_CO_TRANSCRIPT_ADDED,
    EVENT_FINANCIER_TRANSCRIPT_ADDED,
    EVENT_SUPPLIER_TRANSCRIPT_ADDED,
    emit_ai_event,
)
from operator_crm import PROVENANCE_OPERATOR_REPORTED, promote_transcript_fact

TRANSCRIPT_SUPPLIER = "SUPPLIER"
TRANSCRIPT_FINANCIER = "FINANCIER"
TRANSCRIPT_CO = "CONTRACTING_OFFICER"
TRANSCRIPT_OTHER = "OTHER"

ANALYSIS_NOT_ANALYZED = "NOT_ANALYZED"
ANALYSIS_PENDING = "PENDING"
ANALYSIS_COMPLETE = "COMPLETE"

VALID_TYPES = frozenset({TRANSCRIPT_SUPPLIER, TRANSCRIPT_FINANCIER, TRANSCRIPT_CO, TRANSCRIPT_OTHER})


def paste_transcript(
    session: Any,
    *,
    contract_id: int,
    raw_transcript: str,
    transcript_type: str = TRANSCRIPT_SUPPLIER,
    operator: str | None = None,
    organization_name: str | None = None,
    supplier_id: int | None = None,
    contact_id: int | None = None,
    provider_id: int | None = None,
    government_contact_id: int | None = None,
    called_at: datetime | None = None,
    external_call_id: str | None = None,
    enqueue_ai: bool = False,
) -> dict[str, Any]:
    from models import CallTranscript

    ttype = str(transcript_type or TRANSCRIPT_SUPPLIER).upper()
    if ttype not in VALID_TYPES:
        ttype = TRANSCRIPT_OTHER

    row = CallTranscript(
        contract_id=contract_id,
        supplier_id=supplier_id,
        contact_id=contact_id,
        provider_id=provider_id,
        government_contact_id=government_contact_id,
        transcript_type=ttype,
        organization_name=organization_name,
        operator=operator,
        analysis_status=ANALYSIS_NOT_ANALYZED,
        source="MANUAL",
        external_call_id=external_call_id,
        raw_transcript=raw_transcript or "",
        called_at=called_at or now_utc(),
    )
    session.add(row)
    session.flush()

    event_map = {
        TRANSCRIPT_SUPPLIER: EVENT_SUPPLIER_TRANSCRIPT_ADDED,
        TRANSCRIPT_FINANCIER: EVENT_FINANCIER_TRANSCRIPT_ADDED,
        TRANSCRIPT_CO: EVENT_CO_TRANSCRIPT_ADDED,
    }
    job = None
    if enqueue_ai:
        job = emit_ai_event(
            session,
            trigger_event=event_map.get(ttype, EVENT_SUPPLIER_TRANSCRIPT_ADDED),
            contract_id=contract_id,
            payload={"transcript_id": row.id},
        )

    return {
        "id": row.id,
        "transcript_type": ttype,
        "analysis_status": ANALYSIS_NOT_ANALYZED,
        "external_call_id": external_call_id,
        "ai_job": job,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def list_transcripts(session: Any, contract_id: int) -> list[dict[str, Any]]:
    from models import CallTranscript

    rows = (
        session.query(CallTranscript)
        .filter_by(contract_id=contract_id)
        .order_by(CallTranscript.created_at.desc())
        .all()
    )
    return [transcript_to_dict(r) for r in rows]


def transcript_to_dict(row: Any) -> dict[str, Any]:
    analysis = build_transcript_analysis_contract(row)
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "transcript_type": row.transcript_type,
        "organization_name": row.organization_name,
        "operator": row.operator,
        "source": row.source,
        "external_call_id": row.external_call_id,
        "analysis_status": row.analysis_status,
        "called_at": row.called_at.isoformat() if row.called_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "raw_transcript_preview": (row.raw_transcript or "")[:200],
        "analysis": analysis,
    }


def build_transcript_analysis_contract(row: Any) -> dict[str, Any]:
    """Future AI contract — deterministic stub when NOT_ANALYZED."""
    if row.analysis_status == ANALYSIS_NOT_ANALYZED:
        return {
            "status": "NOT_ANALYZED",
            "ui_message": "ANALYSIS_AVAILABLE_LATER",
            "summary": None,
            "statements_made": [],
            "potential_new_facts": [],
            "questions_answered": [],
            "questions_still_unanswered": [],
            "contradictions": [],
            "risks": [],
            "economic_impact": [],
            "documentation_still_required": [],
            "recommended_follow_up": [],
            "suggested_next_questions": [],
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
        }
    stored = row.ai_extraction_json if isinstance(row.ai_extraction_json, dict) else {}
    return {**stored, "status": row.analysis_status}


def record_transcript_statement(
    *,
    statement: str,
    transcript_id: int,
    field: str,
    value: Any,
) -> dict[str, Any]:
    """
    Transcript statements NEVER become VERIFIED facts automatically.
    Example: lender said no PG -> TRANSCRIPT_REPORTED / UNVERIFIED.
    """
    promo = promote_transcript_fact(current_status="TRANSCRIPT_RAW", target_status="VERIFIED")
    return {
        "statement": statement,
        "transcript_id": transcript_id,
        "extracted": {
            "field": field,
            "value": value,
            "source": "CALL_TRANSCRIPT",
            "status": "TRANSCRIPT_REPORTED",
            "verification": "UNVERIFIED",
        },
        "may_promote_to_verified": promo.get("allowed") is True,
        "promotion_blocked_reason": promo.get("reason"),
    }
