"""Product opportunity funnel orchestrator — resumable, idempotent, cache-aware.

Paid analysis only for CORE_PRODUCT (or explicit operator override).
No automatic paid runs on SECONDARY_SERVICE.
"""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from ai_funnel import stage0_evaluate
from ai_stage1 import resolve_current_stage1_result, run_stage1_triage
from ai_stage2 import run_stage2_evidence
from deal_engine import (
    DECISION_NEEDS_RESEARCH,
    build_deal_economics,
    compute_deal_score,
    decide_product_deal,
    evaluate_financing_execution_gate,
    evaluate_portfolio_capacity,
)
from knowledge_store import persist_product_requirements, upsert_deal_state
from product_deal import (
    FIT_CORE_PRODUCT,
    FIT_SECONDARY_SERVICE,
    FIT_UNKNOWN,
    paid_work_priority_rank,
    plan_product_research,
    resolve_core_fit,
)
from stage3_engine import stage3_preflight


def funnel_preflight(opportunity: Any, *, session: Any = None) -> dict[str, Any]:
    """Stage 0 + cache status + estimated costs — NO paid OpenAI."""
    stage0 = stage0_evaluate(opportunity)
    fit = resolve_core_fit(stage0_classification=stage0.get("classification"))
    s1 = resolve_current_stage1_result(opportunity, stage0=stage0)
    s1_hit = bool(s1.get("current") and s1.get("result"))

    # Stage 2 cache: probe via fingerprint only if Stage 1 present — no API
    s2_cache = {"status": "UNKNOWN", "note": "probe_on_run"}
    if s1_hit:
        s2_cache = {"status": "CHECK_ON_RUN", "stage1_fingerprint": s1.get("fingerprint")}

    return {
        "opportunity_id": getattr(opportunity, "id", None),
        "notice_id": getattr(opportunity, "notice_id", None),
        "stage0": {
            "decision": stage0.get("decision"),
            "classification": stage0.get("classification"),
            "advance": stage0.get("advance"),
        },
        "core_fit": fit["core_fit"],
        "product_purity": fit.get("product_purity"),
        "paid_work_priority_rank": paid_work_priority_rank(fit),
        "stage1_cache": {
            "hit": s1_hit,
            "fingerprint": s1.get("fingerprint"),
            "status": s1.get("status"),
        },
        "stage2_cache": s2_cache,
        "estimated_ai_cost_usd": None,
        "estimated_ai_cost_note": "Exact cost unknown until model run; cache hits = $0",
        "research_readiness": fit["core_fit"] == FIT_CORE_PRODUCT,
        "paid_analysis_allowed_automatically": fit["core_fit"] == FIT_CORE_PRODUCT,
        "LIVE_API_REQUESTS": 0,
        "openai_called": False,
    }


def run_product_funnel(
    opportunity: Any,
    *,
    session: Any = None,
    authorize_stage1: bool = False,
    authorize_stage2: bool = False,
    authorize_stage3_external: bool = False,
    persist: bool = True,
    operator_bid_amount: float | None = None,
    bid_amount_verified: bool = False,
    allow_secondary_service_paid: bool = False,
) -> dict[str, Any]:
    """
    Process ONE opportunity through the product funnel.

    Idempotent: uses Stage 1/2 caches. Will not pay for SECONDARY_SERVICE
    unless allow_secondary_service_paid=True (explicit override).
    """
    checkpoint: dict[str, Any] = {"steps": []}
    ai_cost = 0.0

    stage0 = stage0_evaluate(opportunity)
    fit = resolve_core_fit(stage0_classification=stage0.get("classification"))
    checkpoint["steps"].append({"step": "stage0", "classification": stage0.get("classification"), "core_fit": fit["core_fit"]})

    if stage0.get("decision") == "REJECT":
        decision = {
            "decision": "REJECT",
            "reason_codes": stage0.get("reject_reasons") or ["STAGE0_REJECT"],
            "bid_eligible": False,
        }
        out = _finalize(
            opportunity,
            session,
            persist,
            stage0=stage0,
            fit=fit,
            decision=decision,
            checkpoint=checkpoint,
            ai_cost=ai_cost,
            pipeline_stage="rejected_stage0",
        )
        out["LIVE_API_REQUESTS"] = 0
        return out

    if fit["core_fit"] == FIT_SECONDARY_SERVICE and not allow_secondary_service_paid:
        decision = {
            "decision": "WATCH",
            "reason_codes": ["SECONDARY_SERVICE_SKIP_PAID_FUNNEL"],
            "bid_eligible": False,
        }
        out = _finalize(
            opportunity,
            session,
            persist,
            stage0=stage0,
            fit=fit,
            decision=decision,
            checkpoint=checkpoint,
            ai_cost=0.0,
            pipeline_stage="secondary_service_watch",
        )
        out["LIVE_API_REQUESTS"] = 0
        out["note"] = "Service functionality preserved; paid product funnel not auto-run"
        return out

    # Stage 1 — resolve cache first
    s1_res = resolve_current_stage1_result(opportunity, stage0=stage0)
    stage1 = s1_res.get("result")
    if not s1_res.get("current"):
        if not authorize_stage1:
            out = _finalize(
                opportunity,
                session,
                persist,
                stage0=stage0,
                fit=fit,
                decision={
                    "decision": DECISION_NEEDS_RESEARCH,
                    "reason_codes": ["STAGE1_AUTHORIZATION_REQUIRED"],
                    "bid_eligible": False,
                },
                checkpoint=checkpoint,
                ai_cost=ai_cost,
                pipeline_stage="needs_stage1",
            )
            out["LIVE_API_REQUESTS"] = 0
            out["stage1_resolution"] = s1_res
            return out
        stage1 = run_stage1_triage(opportunity, stage0=stage0, automatic=False)
        ai_cost += float(stage1.get("incremental_cost_usd") or 0)
        checkpoint["steps"].append({"step": "stage1_live", "cache_hit": stage1.get("cache_hit"), "cost": stage1.get("incremental_cost_usd")})
        # Live Stage 1 writes analysis cache — re-resolve so Stage 2 gate sees current HIT
        s1_res = resolve_current_stage1_result(opportunity, stage0=stage0)
        if s1_res.get("current") and s1_res.get("result"):
            stage1 = s1_res["result"]
        # Re-resolve fit from Stage 1 category
        fit = resolve_core_fit(
            stage1_category=stage1.get("category"),
            stage0_classification=stage0.get("classification"),
        )
    else:
        checkpoint["steps"].append({"step": "stage1_cache", "fingerprint": s1_res.get("fingerprint")})
        fit = resolve_core_fit(
            stage1_category=(stage1 or {}).get("category"),
            stage0_classification=stage0.get("classification"),
        )

    if fit["core_fit"] != FIT_CORE_PRODUCT and not allow_secondary_service_paid:
        out = _finalize(
            opportunity,
            session,
            persist,
            stage0=stage0,
            stage1=stage1,
            fit=fit,
            decision={
                "decision": "WATCH",
                "reason_codes": ["NOT_CORE_PRODUCT_AFTER_STAGE1"],
                "bid_eligible": False,
            },
            checkpoint=checkpoint,
            ai_cost=ai_cost,
            pipeline_stage="not_core_after_stage1",
        )
        out["LIVE_API_REQUESTS"] = 0 if not authorize_stage1 else "see_incremental"
        return out

    # Stage 2
    if not authorize_stage2:
        out = _finalize(
            opportunity,
            session,
            persist,
            stage0=stage0,
            stage1=stage1,
            fit=fit,
            decision={
                "decision": DECISION_NEEDS_RESEARCH,
                "reason_codes": ["STAGE2_AUTHORIZATION_REQUIRED"],
                "bid_eligible": False,
            },
            checkpoint=checkpoint,
            ai_cost=ai_cost,
            pipeline_stage="needs_stage2",
        )
        out["LIVE_API_REQUESTS"] = 0
        return out

    stage2 = run_stage2_evidence(
        opportunity,
        stage0=stage0,
        stage1=stage1,
        stage1_resolution=s1_res if s1_res.get("current") else None,
        automatic=False,
    )
    ai_cost += float(stage2.get("incremental_cost_usd") or 0)
    checkpoint["steps"].append(
        {
            "step": "stage2",
            "cache_hit": stage2.get("cache_hit"),
            "reason_code": stage2.get("reason_code"),
            "cost": stage2.get("incremental_cost_usd"),
        }
    )

    if persist and session is not None and getattr(opportunity, "id", None):
        persist_product_requirements(
            session,
            int(opportunity.id),
            stage2.get("product_requirements") or {},
            notice_id=getattr(opportunity, "notice_id", None),
        )

    # Stage 3 plan (postgres-first, no external)
    research_plan = plan_product_research(
        opportunity, stage0=stage0, stage1=stage1, stage2=stage2, session=session
    )
    s3_pre = stage3_preflight(
        opportunity, stage0=stage0, stage1=stage1, stage2=stage2, session=session
    )
    checkpoint["steps"].append({"step": "stage3_plan", "tasks": len(research_plan.get("research_tasks") or [])})

    # Economics / gates / decision (no invented supplier prices)
    costs = (stage2.get("economic_requirements") or {}).get("costs") or {}
    economics = build_deal_economics(
        operator_bid_amount=operator_bid_amount,
        bid_amount_verified=bid_amount_verified,
        costs=costs,
    )
    financing_gate = evaluate_financing_execution_gate(financing_term=None, term_verified=False)
    portfolio = evaluate_portfolio_capacity()
    decision = decide_product_deal(
        core_fit=fit["core_fit"],
        economics=economics,
        financing_gate=financing_gate,
        match_result=None,
        research_tasks=research_plan.get("research_tasks"),
        set_aside_eligible=stage0.get("set_aside_eligible"),
    )
    deal_score = compute_deal_score(
        economics=economics,
        decision=decision,
        financing_gate=financing_gate,
    )

    out = _finalize(
        opportunity,
        session,
        persist,
        stage0=stage0,
        stage1=stage1,
        stage2=stage2,
        fit=fit,
        decision=decision,
        economics=economics,
        financing_gate=financing_gate,
        deal_score=deal_score,
        portfolio=portfolio,
        research_plan=research_plan,
        checkpoint=checkpoint,
        ai_cost=ai_cost,
        pipeline_stage=_pipeline_from_decision(decision),
    )
    out["stage3_preflight"] = s3_pre
    out["authorize_stage3_external"] = authorize_stage3_external
    out["LIVE_OPENAI_NOTE"] = "Only Stage 1/2 calls if cache miss and authorized"
    return out


def _pipeline_from_decision(decision: dict[str, Any]) -> str:
    d = decision.get("decision")
    if d == "BID":
        return "bid_ready"
    if d == "WATCH":
        return "watch"
    if d == "REJECT":
        return "rejected"
    return "needs_research"


def _finalize(
    opportunity: Any,
    session: Any,
    persist: bool,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage2: dict[str, Any] | None = None,
    fit: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    financing_gate: dict[str, Any] | None = None,
    deal_score: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    research_plan: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    ai_cost: float = 0.0,
    pipeline_stage: str | None = None,
) -> dict[str, Any]:
    fit = fit or {}
    decision = decision or {}
    payload = {
        "opportunity_id": getattr(opportunity, "id", None),
        "notice_id": getattr(opportunity, "notice_id", None),
        "core_fit": fit.get("core_fit"),
        "product_purity": fit.get("product_purity"),
        "stage0": stage0,
        "stage1_summary": {
            "category": (stage1 or {}).get("category"),
            "reason_code": (stage1 or {}).get("reason_code"),
            "core_fit": (stage1 or {}).get("core_fit"),
        }
        if stage1
        else None,
        "stage2_summary": {
            "reason_code": (stage2 or {}).get("reason_code"),
            "cache_hit": (stage2 or {}).get("cache_hit"),
            "core_fit": (stage2 or {}).get("core_fit"),
        }
        if stage2
        else None,
        "product_requirements": (stage2 or {}).get("product_requirements"),
        "research_plan": research_plan,
        "economics": economics,
        "financing_gate": financing_gate,
        "deal_score": deal_score,
        "portfolio": portfolio,
        "decision": decision.get("decision"),
        "reason_codes": decision.get("reason_codes"),
        "pipeline_stage": pipeline_stage,
        "funnel_checkpoint": checkpoint,
        "ai_cost_usd": ai_cost,
        "completed_at": now_utc().isoformat(),
    }
    if persist and session is not None and getattr(opportunity, "id", None):
        upsert_deal_state(
            session,
            int(opportunity.id),
            {
                "core_fit": fit.get("core_fit"),
                "pipeline_stage": pipeline_stage,
                "decision": decision.get("decision"),
                "reason_codes": decision.get("reason_codes"),
                "economics": economics,
                "deal_score": deal_score,
                "portfolio": portfolio,
                "financing_gate": financing_gate,
                "research_plan": research_plan,
                "funnel_checkpoint": checkpoint,
                "ai_cost_usd": ai_cost,
            },
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
    return payload
