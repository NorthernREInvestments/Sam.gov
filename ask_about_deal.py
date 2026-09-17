"""Ask About This Deal — context preview + audit, no OpenAI in this build."""

from __future__ import annotations

from typing import Any

from ai_events import EVENT_ASK_ABOUT_DEAL, emit_ai_event
from deal_context_packet import build_deal_context_packet


def build_context_preview(session: Any, contract_id: int, question: str | None = None) -> dict[str, Any]:
    """Deterministic preview for tests/dev — future AI uses same packet."""
    packet = build_deal_context_packet(session, contract_id, compact=True)
    preview = {
        "contract_id": contract_id,
        "question": question or "",
        "context_sections": list(packet.keys()),
        "deal_readiness_status": (packet.get("deal_readiness") or {}).get("value", {}).get("status"),
        "bid_readiness_status": (packet.get("bid_readiness") or {}).get("value", {}).get("status"),
        "blocker_count": len((packet.get("blockers") or {}).get("value") or []),
        "warning_count": len((packet.get("warnings") or {}).get("value") or []),
        "packet": packet,
        "ai_status": "PREVIEW_ONLY",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }
    return preview


def create_ask_request(
    session: Any,
    *,
    contract_id: int,
    question: str,
    operator: str | None = None,
    enqueue_ai: bool = False,
) -> dict[str, Any]:
    from models import AskAboutDealRequest

    preview = build_context_preview(session, contract_id, question=question)
    row = AskAboutDealRequest(
        contract_id=contract_id,
        question=question,
        operator=operator,
        context_preview_json=preview,
        status="PREVIEW_ONLY",
    )
    session.add(row)
    session.flush()

    job = None
    if enqueue_ai:
        job = emit_ai_event(
            session,
            trigger_event=EVENT_ASK_ABOUT_DEAL,
            contract_id=contract_id,
            payload={"ask_request_id": row.id, "question": question},
        )

    return {
        "request_id": row.id,
        "status": "PREVIEW_ONLY",
        "context_preview": preview,
        "ai_job": job,
        "response_contract": {
            "answer": None,
            "important_findings": [],
            "risks": [],
            "missing_information": [],
            "economic_effects": [],
            "recommended_actions": [],
            "suggested_questions": [],
            "evidence_refs": [],
            "confidence": None,
        },
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


class FutureAskAboutDealAIService:
    """Future interface — not invoked in this build."""

    def answer(self, *, context_packet: dict[str, Any], question: str) -> dict[str, Any]:
        raise NotImplementedError("AI service not wired — PREVIEW_ONLY build")
