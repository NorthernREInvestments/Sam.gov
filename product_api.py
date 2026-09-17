"""Product-deal API routes — no paid AI on GET/page load."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from database import SessionLocal
from models import Contract, DealState, ResearchEvent

router = APIRouter(prefix="/api/product", tags=["product-deal"])


class BidAmountBody(BaseModel):
    amount: float
    verified: bool = False


class DecisionBody(BaseModel):
    decision: str
    reason: str = ""


class FunnelRunBody(BaseModel):
    authorize_stage1: bool = False
    authorize_stage2: bool = False
    authorize_stage3_external: bool = False
    allow_secondary_service_paid: bool = False
    operator_bid_amount: float | None = None
    bid_amount_verified: bool = False


class Stage3RunBody(BaseModel):
    authorize_external: bool = False
    authorize_web: bool = False
    authorize_openai: bool = False


def _get_contract(session, notice_id: str) -> Contract:
    row = session.query(Contract).filter_by(notice_id=notice_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Contract not found")
    return row


@router.get("/pipeline")
def product_pipeline() -> dict[str, Any]:
    """Product pipeline buckets — read-only, no paid AI."""
    from product_deal import FIT_CORE_PRODUCT, resolve_core_fit
    from ai_funnel import stage0_evaluate

    session = SessionLocal()
    try:
        buckets: dict[str, list[dict[str, Any]]] = {
            "new_product": [],
            "needs_stage1": [],
            "needs_stage2": [],
            "needs_research": [],
            "needs_supplier_pricing": [],
            "needs_financing": [],
            "needs_operator_input": [],
            "bid_ready": [],
            "watch": [],
            "rejected": [],
            "submitted": [],
            "awarded": [],
            "secondary_service": [],
        }
        deals = {d.contract_id: d for d in session.query(DealState).all()}
        for c in session.query(Contract).order_by(Contract.id.desc()).limit(200):
            s0 = stage0_evaluate(c)
            fit = resolve_core_fit(stage0_classification=s0.get("classification"))
            deal = deals.get(c.id)
            card = {
                "id": c.id,
                "notice_id": c.notice_id,
                "title": c.title,
                "core_fit": fit["core_fit"],
                "pipeline_stage": (deal.pipeline_stage if deal else None) or "new",
                "decision": deal.decision if deal else None,
                "due_date": c.due_date.isoformat() if c.due_date else None,
                "naics_code": c.naics_code,
                "status": c.status,
            }
            try:
                from sam_scarcity import opportunity_sam_dashboard_fields

                card.update(opportunity_sam_dashboard_fields(c))
            except Exception:
                card["SAM_API_ELIGIBILITY"] = "UNKNOWN"
                card["LIVE_API_REQUESTS"] = 0
            stage = card["pipeline_stage"]
            if fit["core_fit"] != FIT_CORE_PRODUCT:
                buckets["secondary_service"].append(card)
                continue
            if c.status and str(c.status).lower() in {"awarded", "won"}:
                buckets["awarded"].append(card)
            elif c.status and str(c.status).lower() in {"submitted", "bid_submitted"}:
                buckets["submitted"].append(card)
            elif stage == "bid_ready" or (deal and deal.decision == "BID"):
                buckets["bid_ready"].append(card)
            elif stage == "watch" or (deal and deal.decision == "WATCH"):
                buckets["watch"].append(card)
            elif stage == "rejected" or (deal and deal.decision == "REJECT"):
                buckets["rejected"].append(card)
            elif stage == "needs_stage1":
                buckets["needs_stage1"].append(card)
            elif stage == "needs_stage2":
                buckets["needs_stage2"].append(card)
            elif stage == "needs_research":
                reasons = list(deal.reason_codes_json or []) if deal else []
                if any("SUPPLIER" in str(r) or "QUOTE" in str(r) for r in reasons):
                    buckets["needs_supplier_pricing"].append(card)
                elif any("FINANCING" in str(r) for r in reasons):
                    buckets["needs_financing"].append(card)
                else:
                    buckets["needs_research"].append(card)
            else:
                buckets["new_product"].append(card)
        return {
            "LIVE_API_REQUESTS": 0,
            "paid_ai_on_load": False,
            "buckets": buckets,
            "counts": {k: len(v) for k, v in buckets.items()},
        }
    finally:
        session.close()


@router.get("/opportunities/{notice_id}/deal")
def product_deal_detail(notice_id: str) -> dict[str, Any]:
    """Opportunity deal detail — read-only assembly, no paid AI."""
    from ai_funnel import stage0_evaluate
    from historical_context import get_historical_context_for_opportunity
    from product_deal import resolve_core_fit
    from product_funnel import funnel_preflight

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        s0 = stage0_evaluate(c)
        fit = resolve_core_fit(stage0_classification=s0.get("classification"))
        deal = session.query(DealState).filter_by(contract_id=c.id).first()
        events = (
            session.query(ResearchEvent)
            .filter_by(contract_id=c.id)
            .order_by(ResearchEvent.id.desc())
            .limit(50)
            .all()
        )
        pre = funnel_preflight(c, session=session)
        hist = get_historical_context_for_opportunity(c, session=session, include_gs_lookup=False)
        return {
            "LIVE_API_REQUESTS": 0,
            "paid_ai_on_load": False,
            "notice_id": c.notice_id,
            "opportunity_id": c.id,
            "title": c.title,
            "agency": c.agency,
            "deadline": c.due_date.isoformat() if c.due_date else None,
            "set_aside": c.set_aside,
            "naics": c.naics_code,
            "psc": (c.sam_raw or {}).get("classificationCode") if isinstance(c.sam_raw, dict) else None,
            "source": "FEDERAL",
            "core_fit": fit,
            "stage0": {
                "decision": s0.get("decision"),
                "classification": s0.get("classification"),
                "status": "POLICY" if fit else None,
            },
            "funnel_preflight": pre,
            "deal_state": {
                "pipeline_stage": deal.pipeline_stage if deal else None,
                "decision": deal.decision if deal else None,
                "reason_codes": deal.reason_codes_json if deal else [],
                "economics": deal.economics_json if deal else None,
                "deal_score": deal.deal_score_json if deal else None,
                "portfolio": deal.portfolio_json if deal else None,
                "financing_gate": deal.financing_gate_json if deal else None,
                "research_plan": deal.research_plan_json if deal else None,
                "operator_bid_amount": float(deal.operator_bid_amount)
                if deal and deal.operator_bid_amount is not None
                else None,
                "ai_cost_usd": float(deal.ai_cost_usd) if deal and deal.ai_cost_usd is not None else None,
            }
            if deal
            else None,
            "historical": hist,
            "research_events": [
                {
                    "id": e.id,
                    "task_code": e.task_code,
                    "result_status": e.result_status,
                    "source": e.source,
                    "cost_usd": float(e.cost_usd) if e.cost_usd is not None else None,
                    "executed": e.executed,
                    "authorized": e.authorized,
                    "cache_hit": e.cache_hit,
                    "retrieved_at": e.retrieved_at.isoformat() if e.retrieved_at else None,
                }
                for e in events
            ],
            "status_legend": {
                "VERIFIED": "Fact established with evidence",
                "CALCULATED": "Derived from verified inputs",
                "ASSESSMENT": "Not a procurement fact",
                "HISTORICAL": "Past context — not current",
                "UNKNOWN": "Does not exist as a fact",
                "POLICY": "Business-policy classification",
            },
        }
    finally:
        session.close()


@router.get("/opportunities/{notice_id}/funnel-preflight")
def api_funnel_preflight(notice_id: str) -> dict[str, Any]:
    from product_funnel import funnel_preflight

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        return funnel_preflight(c, session=session)
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/funnel-run")
def api_funnel_run(notice_id: str, body: FunnelRunBody) -> dict[str, Any]:
    """Explicit one-opportunity funnel — paid only when authorize flags set."""
    from product_funnel import run_product_funnel

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        return run_product_funnel(
            c,
            session=session,
            authorize_stage1=body.authorize_stage1,
            authorize_stage2=body.authorize_stage2,
            authorize_stage3_external=body.authorize_stage3_external,
            allow_secondary_service_paid=body.allow_secondary_service_paid,
            operator_bid_amount=body.operator_bid_amount,
            bid_amount_verified=body.bid_amount_verified,
            persist=True,
        )
    finally:
        session.close()


@router.get("/opportunities/{notice_id}/stage3-preflight")
def api_stage3_preflight(notice_id: str) -> dict[str, Any]:
    from product_funnel import funnel_preflight
    from stage3_engine import stage3_preflight
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        s0 = stage0_evaluate(c)
        s1 = resolve_current_stage1_result(c, stage0=s0)
        return stage3_preflight(
            c,
            stage0=s0,
            stage1=s1.get("result"),
            stage2=None,
            session=session,
        )
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/stage3-run")
def api_stage3_run(notice_id: str, body: Stage3RunBody) -> dict[str, Any]:
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from stage3_engine import run_stage3

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        s0 = stage0_evaluate(c)
        s1 = resolve_current_stage1_result(c, stage0=s0)
        result = run_stage3(
            c,
            stage0=s0,
            stage1=s1.get("result"),
            session=session,
            authorize_external=body.authorize_external,
            authorize_web=body.authorize_web,
            authorize_openai=body.authorize_openai,
            persist_events=True,
        )
        session.commit()
        return result
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/bid-amount")
def api_set_bid_amount(notice_id: str, body: BidAmountBody) -> dict[str, Any]:
    from deal_engine import build_deal_economics, decide_product_deal, evaluate_financing_execution_gate
    from knowledge_store import upsert_deal_state
    from product_deal import resolve_core_fit
    from ai_funnel import stage0_evaluate

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        deal = session.query(DealState).filter_by(contract_id=c.id).first()
        costs = ((deal.economics_json or {}).get("costs") if deal and deal.economics_json else {}) or {}
        economics = build_deal_economics(
            operator_bid_amount=body.amount,
            bid_amount_verified=body.verified,
            costs=costs if isinstance(costs, dict) else {},
        )
        s0 = stage0_evaluate(c)
        fit = resolve_core_fit(stage0_classification=s0.get("classification"))
        fin = (deal.financing_gate_json if deal else None) or evaluate_financing_execution_gate()
        decision = decide_product_deal(
            core_fit=fit["core_fit"],
            economics=economics,
            financing_gate=fin,
            research_tasks=((deal.research_plan_json or {}).get("research_tasks") if deal and deal.research_plan_json else []),
        )
        upsert_deal_state(
            session,
            c.id,
            {
                "operator_bid_amount": body.amount,
                "economics": economics,
                "decision": decision.get("decision"),
                "reason_codes": decision.get("reason_codes"),
                "financing_gate": fin,
            },
        )
        session.commit()
        return {"ok": True, "economics": economics, "decision": decision, "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/mark-watch")
def api_mark_watch(notice_id: str, body: DecisionBody) -> dict[str, Any]:
    from knowledge_store import upsert_deal_state

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        upsert_deal_state(
            session,
            c.id,
            {
                "decision": "WATCH",
                "pipeline_stage": "watch",
                "reason_codes": [body.reason or "OPERATOR_WATCH"],
            },
        )
        session.commit()
        return {"ok": True, "decision": "WATCH", "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/reject")
def api_reject(notice_id: str, body: DecisionBody) -> dict[str, Any]:
    from knowledge_store import upsert_deal_state

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        upsert_deal_state(
            session,
            c.id,
            {
                "decision": "REJECT",
                "pipeline_stage": "rejected",
                "reason_codes": [body.reason or "OPERATOR_REJECT"],
            },
        )
        session.commit()
        return {"ok": True, "decision": "REJECT", "LIVE_API_REQUESTS": 0}
    finally:
        session.close()


@router.post("/opportunities/{notice_id}/generate-proposal")
def api_generate_proposal_bridge(notice_id: str) -> dict[str, Any]:
    """Bridge BID-ready deals into existing proposal workflow — does not auto-submit."""
    from proposal_service import build_proposal_readiness

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        deal = session.query(DealState).filter_by(contract_id=c.id).first()
        if not deal or deal.decision != "BID":
            raise HTTPException(
                status_code=400,
                detail="Proposal bridge requires deal decision BID — operator must pass gates first",
            )
        readiness = build_proposal_readiness(c, {})
        # Do not auto-call generate_proposal AI here without explicit separate action in proposal UI
        return {
            "ok": True,
            "LIVE_API_REQUESTS": 0,
            "proposal_ai_invoked": False,
            "readiness": readiness,
            "operator_control": True,
            "note": (
                "Use existing proposal UI to generate narrative. "
                "Verified facts vs calculated pricing vs operator strategy must stay distinct."
            ),
            "fact_layers": {
                "verified_solicitation": "from Stage 2 / attachments",
                "verified_product": "from gt_knowledge_products / requirements",
                "calculated_pricing": "from deal economics",
                "operator_bid_strategy": float(deal.operator_bid_amount)
                if deal.operator_bid_amount is not None
                else None,
                "ai_narrative": "not auto-generated here",
            },
        }
    finally:
        session.close()


@router.get("/discovery/preflight")
def api_discovery_preflight(
    max_naics: int = Query(3, ge=1, le=20),
    limit_per_naics: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    from product_discovery import preflight_product_sync

    return preflight_product_sync(limit_per_naics=limit_per_naics, max_naics=max_naics)


@router.get("/cost-summary")
def api_cost_summary() -> dict[str, Any]:
    """Observability snapshot — no paid calls / no SAM."""
    from ai_cost_budget import get_cost_snapshot
    from api_budget import get_usage_snapshot
    from sam_scarcity import SOURCE_ROUTING_ORDER, sam_budget_snapshot, sam_scarcity_mode

    out: dict[str, Any] = {
        "LIVE_API_REQUESTS": 0,
        "paid_ai_on_load": False,
        "sam_api_on_load": False,
        "sam_scarcity_mode": sam_scarcity_mode(),
        "source_routing_order": list(SOURCE_ROUTING_ORDER),
        "sam_budget": sam_budget_snapshot(),
    }
    try:
        out["ai_budget"] = get_cost_snapshot()
    except Exception as exc:
        out["ai_budget_error"] = str(exc)
    try:
        out["api_usage"] = get_usage_snapshot()
    except Exception as exc:
        out["api_usage_error"] = str(exc)
    session = SessionLocal()
    try:
        out["research_events_count"] = session.query(ResearchEvent).count()
        out["deal_states_count"] = session.query(DealState).count()
        try:
            from models import SamApiAudit

            out["sam_api_audit_count"] = session.query(SamApiAudit).count()
        except Exception:
            out["sam_api_audit_count"] = 0
    finally:
        session.close()
    return out


@router.get("/sam-eligibility/{notice_id}")
def api_sam_eligibility(notice_id: str) -> dict[str, Any]:
    """Read-only SAM eligibility — never calls SAM."""
    from sam_scarcity import PURPOSE_DASHBOARD_LOAD, evaluate_sam_api_eligibility, opportunity_sam_dashboard_fields

    session = SessionLocal()
    try:
        c = _get_contract(session, notice_id)
        # Prove dashboard purpose is NOT_NEEDED
        dash = evaluate_sam_api_eligibility(c, purpose=PURPOSE_DASHBOARD_LOAD)
        fields = opportunity_sam_dashboard_fields(c)
        return {
            **fields,
            "dashboard_purpose_check": dash,
            "LIVE_API_REQUESTS": 0,
            "sam_api_on_load": False,
        }
    finally:
        session.close()


@router.get("/sources")
def api_sources() -> dict[str, Any]:
    from opportunity_source import NEXT_CONNECTORS, SOURCE_CATEGORIES, list_sources
    from sam_scarcity import SOURCE_ROUTING_ORDER

    return {
        "registered": list_sources(),
        "categories": list(SOURCE_CATEGORIES),
        "next_connectors": list(NEXT_CONNECTORS),
        "source_routing_order": list(SOURCE_ROUTING_ORDER),
        "LIVE_API_REQUESTS": 0,
    }
