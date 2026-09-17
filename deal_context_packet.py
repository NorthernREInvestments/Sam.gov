"""Canonical DealContextPacket for future AI copilot — Postgres-first, compact."""

from __future__ import annotations

from typing import Any

FACT_VERIFIED = "VERIFIED_FACT"
FACT_CALCULATED = "CALCULATED_VALUE"
FACT_OPERATOR = "OPERATOR_REPORTED"
FACT_TRANSCRIPT = "TRANSCRIPT_REPORTED"
FACT_AI = "AI_ASSESSMENT"
FACT_UNKNOWN = "UNKNOWN"
FACT_POLICY = "POLICY"
FACT_SCENARIO = "SCENARIO"


def _field(value: Any, *, classification: str, source: str | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "classification": classification,
        "source": source,
    }


def build_deal_context_packet(session: Any, contract_id: int, *, compact: bool = True) -> dict[str, Any]:
    """Assemble classified context from durable stores — copilot must not rediscover."""
    from deal_workspace import build_workspace_snapshot
    from funding_persistence import load_persisted_funding
    from models import AwardLifecycle, CallTranscript, Contract, DealState

    contract = session.query(Contract).filter_by(id=contract_id).first()
    if not contract:
        return {"error": "contract_not_found", "LIVE_API_REQUESTS": 0}
    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    ws = build_workspace_snapshot(session, contract)
    persisted = load_persisted_funding(session, contract_id)
    award = session.query(AwardLifecycle).filter_by(contract_id=contract_id).first()
    transcripts = (
        session.query(CallTranscript)
        .filter_by(contract_id=contract_id)
        .order_by(CallTranscript.created_at.desc())
        .limit(10 if compact else 50)
        .all()
    )

    econ = ws.get("economics") or {}
    actual_profit = econ.get("actual_profit")
    packet = {
        "contract_id": contract_id,
        "opportunity": _field(
            {
                "id": contract.id,
                "title": contract.title,
                "agency": contract.agency,
                "deadline": contract.due_date.isoformat() if contract.due_date else None,
                "notice_id": contract.notice_id,
            },
            classification=FACT_VERIFIED if contract.notice_id else FACT_OPERATOR,
            source="gt_contracts",
        ),
        "source": _field(deal.core_fit if deal else None, classification=FACT_CALCULATED, source="deal_state"),
        "solicitation_package": _field(
            ws.get("solicitation_package"),
            classification=FACT_CALCULATED,
            source="solicitation_package_engine",
        ),
        "documents": _field(
            ws.get("documents")[:5] if compact else ws.get("documents"),
            classification=FACT_VERIFIED,
            source="gt_solicitation_documents",
        ),
        "requirements": _field(
            ws.get("requirements")[:20] if compact else ws.get("requirements"),
            classification=FACT_VERIFIED,
            source="gt_requirement_register",
        ),
        "bom": _field(ws.get("bom"), classification=FACT_VERIFIED, source="deal_checkpoint"),
        "missing_information": _field(ws.get("missing_info"), classification=FACT_CALCULATED, source="missing_info_engine"),
        "suppliers": _field(ws.get("suppliers"), classification=FACT_OPERATOR, source="gt_knowledge_suppliers"),
        "quotes": _field(ws.get("quotes"), classification=FACT_OPERATOR, source="gt_supplier_offers"),
        "quote_comparison": _field(ws.get("quote_comparison"), classification=FACT_CALCULATED, source="quote_validation"),
        "funding_plan": _field(
            persisted or ws.get("funding_plan"),
            classification=FACT_CALCULATED if persisted else FACT_SCENARIO,
            source="gt_funding_plans" if persisted else "funding_engine",
        ),
        "financing": _field(ws.get("financing"), classification=FACT_OPERATOR, source="gt_financing_pursuits"),
        "economics": _field(
            econ,
            classification=FACT_CALCULATED if actual_profit is not None else FACT_UNKNOWN,
            source="commercial_economics",
        ),
        "proposed_bid": _field(
            ws.get("proposed_bid"),
            classification=FACT_OPERATOR,
            source="operator_bid_amount",
        ),
        "deal_readiness": _field(ws.get("deal_readiness"), classification=FACT_CALCULATED, source="deal_readiness"),
        "bid_readiness": _field(ws.get("bid_readiness"), classification=FACT_CALCULATED, source="bid_readiness"),
        "lifecycle": _field(ws.get("lifecycle"), classification=FACT_CALCULATED, source="deal_lifecycle"),
        "pipeline_bucket": _field(ws.get("pipeline_bucket"), classification=FACT_CALCULATED, source="deal_lifecycle"),
        "warnings": _field(ws.get("warnings"), classification=FACT_CALCULATED, source="exception_engine"),
        "blockers": _field(
            (ws.get("deal_readiness") or {}).get("blockers"),
            classification=FACT_CALCULATED,
            source="deal_readiness",
        ),
        "next_actions": _field(ws.get("next_action"), classification=FACT_CALCULATED, source="next_action_engine"),
        "crm_activities": _field(
            ws.get("activities")[:10] if compact else ws.get("activities"),
            classification=FACT_OPERATOR,
            source="gt_crm_activities",
        ),
        "transcripts": _field(
            [
                {
                    "id": t.id,
                    "type": t.transcript_type,
                    "analysis_status": t.analysis_status,
                    "called_at": t.called_at.isoformat() if t.called_at else None,
                }
                for t in transcripts
            ],
            classification=FACT_TRANSCRIPT,
            source="gt_call_transcripts",
        ),
        "award_lifecycle": _field(
            {
                "status": award.lifecycle_status if award else None,
                "submission_reference": award.submission_reference if award else None,
            },
            classification=FACT_OPERATOR if award else FACT_UNKNOWN,
            source="gt_award_lifecycles",
        ),
        "policy": _field(
            {
                "no_pg": True,
                "no_personal_credit": True,
                "personal_cash_upfront": 0,
                "minimum_actual_profit": 10000,
                "gross_retention_policy_pct": 20,
            },
            classification=FACT_POLICY,
            source="company_hard_rules",
        ),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }
    return packet
