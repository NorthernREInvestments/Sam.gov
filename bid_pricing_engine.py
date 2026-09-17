"""Bid assembly + pricing intelligence orchestrator."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from application_clock import now_utc
from bid_pricing_constants import (
    CALC_VERSION,
    CONF_COMMERCIAL_VERIFY,
    CONF_DEFENSIBLE_ESTIMATE,
    CONF_UNKNOWN,
    CONF_VERIFIED_BINDING,
    DRAFT_INCOMPLETE,
    DRAFT_READY,
    FIN_ALLOWANCE,
    FIN_ESTIMATED,
    FIN_VERIFY,
    FREIGHT_ALLOWANCE,
    FREIGHT_ESTIMATED,
    FREIGHT_VERIFY,
    FUTURE_ACTION,
    OPERATOR_FINAL,
    PRICE_STALE,
    PRICING_COMMERCIAL,
    PRICING_READY,
    READY_AUTH_COMMERCIAL,
    READY_OPERATOR_REVIEW,
    REC_BID,
    REC_NEEDS_VERIFY,
    SUBMISSION_BLOCKED,
)
from bid_pricing_invalidation import evaluate_price_freshness, invalidate_pricing_state
from cost_governor import get_cost_governor, research_value_decision
from cost_governor_constants import TIER_1_ACTIVE, TIER_2_QUALIFIED
from draft_bid_assembly import assemble_draft_bid_package, validate_draft_bid_package
from historical_gov_price_benchmark import build_historical_government_price_benchmark
from line_item_pricing import build_line_item_pricing
from msrp_intelligence import compute_msrp_intelligence
from operating_mode import get_operating_mode, mode_snapshot
from pricing_scenarios import (
    break_even_and_max_costs,
    build_pricing_scenarios,
    commercial_verification_targets,
    sensitivity_analysis,
)
from transaction_economics import compute_transaction_economics, profit_floor_config


def authorize_pricing_research(
    *,
    solicitation_id: str,
    question: str,
    could_change_price: bool = True,
    estimated_max_cost: float = 0.2,
    reusable_evidence_sufficient: bool = False,
    governor: Any = None,
) -> dict[str, Any]:
    voi = research_value_decision(
        question=question,
        could_change_pursuit_or_readiness=could_change_price,
        reusable_evidence_sufficient=reusable_evidence_sufficient,
    )
    if not voi.get("allow_paid"):
        return {"authorized": False, "reason": voi.get("reason"), "value": voi, "paid_research": False}
    gov = governor or get_cost_governor()
    return gov.authorize(
        {
            "provider": "sim",
            "action_type": "AI_COMPLETION",
            "deal_id": solicitation_id,
            "priority_tier": TIER_1_ACTIVE,
            "tracked": True,
            "question": question,
            "could_change_decision": could_change_price,
            "estimated_max_cost": estimated_max_cost,
            "idempotency_key": f"pricing:{solicitation_id}:{question[:80]}",
            "reusable_evidence_sufficient": reusable_evidence_sufficient,
        }
    )


def _sum_acquisition(lines: list[dict[str, Any]]) -> tuple[float | None, str]:
    total = 0.0
    any_known = False
    worst = CONF_VERIFIED_BINDING
    rank = {
        CONF_VERIFIED_BINDING: 0,
        "VERIFIED_PUBLIC": 1,
        "RECENT_HISTORICAL": 2,
        CONF_DEFENSIBLE_ESTIMATE: 3,
        "COMPARABLE_ESTIMATE": 4,
        "STALE": 5,
        CONF_UNKNOWN: 6,
        CONF_COMMERCIAL_VERIFY: 6,
    }
    for li in lines:
        v = li.get("acquisition_unit_cost")
        q = li.get("quantity")
        if v is None or q is None:
            return None, CONF_UNKNOWN
        total += float(v) * float(q)
        any_known = True
        conf = str(li.get("acquisition_confidence") or CONF_UNKNOWN)
        if rank.get(conf, 6) > rank.get(worst, 0):
            worst = conf
    return (total if any_known else None), worst


def analyze_bid_pricing(
    *,
    solicitation_id: str,
    line_items: list[dict[str, Any]] | None = None,
    historical_observations: list[dict[str, Any]] | None = None,
    freight_cost: float | None = None,
    freight_confidence: str = CONF_UNKNOWN,
    financing_cost: float | None = None,
    financing_confidence: str = CONF_UNKNOWN,
    transaction_expense: float | None = 0.0,
    risk_allowance: float | None = None,
    risk_protects_against: str | None = None,
    evaluation_basis: str | None = None,
    award_mode: str = "ALL_OR_NONE",
    government_estimate: float | None = None,
    company_facts: dict[str, Any] | None = None,
    required_forms: list[dict[str, Any]] | None = None,
    submission: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    compliance_matrix: dict[str, Any] | None = None,
    delivery_by: str | None = None,
    authorization_document: str | None = None,
    priced_at: str | None = None,
    profit_config: dict[str, Any] | None = None,
    amendments_accounted: bool | None = None,
    freshness_ok: bool = True,
    hard_blocker: bool = False,
    deadline_actionable: bool = True,
    product_compliance_ok: bool = True,
    eligibility_ok: bool = True,
    paid_research_questions: list[str] | None = None,
    prior_audit: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    lines = list(line_items or [])
    cfg = profit_config or profit_floor_config()

    hist = build_historical_government_price_benchmark(historical_observations)
    acq_total, acq_conf = _sum_acquisition(lines)
    qty_total = sum(float(li.get("quantity") or 0) for li in lines) or None

    # Default risk if uncertain acquisition
    risk = risk_allowance
    if risk is None and acq_total is not None and acq_conf in {CONF_DEFENSIBLE_ESTIMATE, "COMPARABLE_ESTIMATE", "STALE"}:
        risk = round(acq_total * 0.03, 2)
        risk_protects_against = risk_protects_against or "acquisition_estimate_uncertainty"
    elif risk is None:
        risk = 0.0

    freight_state = FREIGHT_VERIFY
    if freight_cost is not None:
        freight_state = FREIGHT_ESTIMATED if freight_confidence != CONF_VERIFIED_BINDING else "FREIGHT_VERIFIED"
        if freight_confidence == CONF_DEFENSIBLE_ESTIMATE:
            freight_state = FREIGHT_ALLOWANCE
    fin_state = FIN_VERIFY
    if financing_cost is not None:
        fin_state = FIN_ESTIMATED if financing_confidence != CONF_VERIFIED_BINDING else "FINANCING_VERIFIED"
        if financing_confidence == CONF_DEFENSIBLE_ESTIMATE:
            fin_state = FIN_ALLOWANCE

    msrp = compute_msrp_intelligence(
        line_items=lines,
        acquisition_cost=acq_total,
        historical_gov_unit=hist.get("recency_weighted_estimate") or hist.get("weighted_average") or hist.get("median"),
    )

    hist_by_line = {}
    if hist.get("median") is not None:
        for i, li in enumerate(lines):
            hist_by_line[str(li.get("line_id") or li.get("clin") or f"L{i+1}")] = hist["median"]

    line_pricing = build_line_item_pricing(
        line_items=lines,
        freight_total=freight_cost,
        financing_total=financing_cost,
        expense_total=transaction_expense,
        risk_total=risk,
        award_mode=award_mode,
        historical_unit_by_line=hist_by_line,
        msrp_by_line={str(li.get("line_id") or f"L{i+1}"): li.get("msrp") for i, li in enumerate(lines)},
    )

    scenarios = build_pricing_scenarios(
        acquisition=acq_total,
        freight=freight_cost,
        financing=financing_cost,
        expense=transaction_expense,
        risk=risk,
        historical_benchmark_unit=hist.get("recency_weighted_estimate") or hist.get("median"),
        quantity=qty_total,
        basket_msrp=msrp.get("basket_msrp"),
        government_estimate=government_estimate,
        evaluation_basis=evaluation_basis,
        acquisition_confidence=acq_conf,
        freight_confidence=freight_confidence if freight_cost is not None else CONF_UNKNOWN,
        financing_confidence=financing_confidence if financing_cost is not None else CONF_UNKNOWN,
        profit_config=cfg,
    )

    rec = scenarios.get("recommended")
    bid_total = rec["total_bid"] if rec else None
    if bid_total is None and scenarios.get("scenarios"):
        # Use balanced for draft display even if not "recommended"
        bid_total = scenarios["scenarios"][1]["total_bid"] if len(scenarios["scenarios"]) > 1 else scenarios["scenarios"][0]["total_bid"]

    economics = compute_transaction_economics(
        government_bid_revenue=bid_total,
        product_acquisition_cost=acq_total,
        freight_cost=freight_cost,
        financing_cost=financing_cost,
        transaction_expense=transaction_expense,
        risk_allowance=risk,
        acquisition_confidence=acq_conf,
        freight_confidence=freight_confidence if freight_cost is not None else CONF_UNKNOWN,
        financing_confidence=financing_confidence if financing_cost is not None else CONF_UNKNOWN,
        risk_protects_against=risk_protects_against,
        freight_status_label=freight_state,
        financing_status_label=fin_state,
        profit_config=cfg,
    )

    # Update MSRP with proposed bid
    msrp = compute_msrp_intelligence(
        line_items=lines,
        acquisition_cost=acq_total,
        historical_gov_unit=hist.get("recency_weighted_estimate") or hist.get("median"),
        proposed_bid=bid_total,
    )

    sens = None
    be = break_even_and_max_costs(
        bid_revenue=bid_total,
        freight=freight_cost,
        financing=financing_cost,
        expense=transaction_expense,
        risk=risk,
        acquisition=acq_total,
        target_profit=cfg["minimum_transaction_profit"],
    )
    if bid_total is not None and acq_total is not None and freight_cost is not None and financing_cost is not None:
        sens = sensitivity_analysis(
            base_bid=bid_total,
            acquisition=acq_total,
            freight=freight_cost,
            financing=financing_cost,
            expense=transaction_expense or 0,
            risk=risk or 0,
            profit_config=cfg,
        )

    targets = commercial_verification_targets(
        bid_revenue=bid_total,
        freight=freight_cost,
        financing=financing_cost,
        expense=transaction_expense,
        risk=risk,
        acquisition=acq_total,
        delivery_by=delivery_by,
        authorization_document=authorization_document,
        target_profit=cfg["minimum_transaction_profit"],
    )

    price_fresh = evaluate_price_freshness(priced_at=priced_at or now_utc().isoformat())

    draft = assemble_draft_bid_package(
        solicitation_id=solicitation_id,
        company_facts=company_facts,
        line_pricing=line_pricing,
        recommended_bid=rec or {"total_bid": bid_total},
        submission=submission,
        delivery=delivery,
        compliance_matrix=compliance_matrix,
        required_forms=required_forms,
        commercial_targets=targets,
        amendments_accounted=amendments_accounted,
    )

    validation = validate_draft_bid_package(
        draft,
        pricing=line_pricing,
        freshness_ok=freshness_ok,
        price_freshness_blocks=price_fresh.get("blocks_final_pricing_readiness", False),
        hard_blocker=hard_blocker,
        deadline_actionable=deadline_actionable,
        product_compliance_ok=product_compliance_ok,
        eligibility_ok=eligibility_ok,
        submission_method_known=bool((submission or {}).get("submission_method")),
        pricing_evidence_current=price_fresh.get("state") not in {PRICE_STALE, "PRICE_EXPIRED"},
    )

    # Finalization state
    if not deadline_actionable or hard_blocker:
        final_state = SUBMISSION_BLOCKED
    elif scenarios.get("recommendation_status") == REC_NEEDS_VERIFY or acq_conf in {CONF_UNKNOWN, CONF_COMMERCIAL_VERIFY}:
        final_state = PRICING_COMMERCIAL
    elif price_fresh.get("blocks_final_pricing_readiness"):
        final_state = PRICING_COMMERCIAL
    elif not validation["passed"]:
        final_state = DRAFT_INCOMPLETE
    elif targets.get("actions"):
        final_state = READY_AUTH_COMMERCIAL
    else:
        final_state = READY_OPERATOR_REVIEW

    if final_state == READY_OPERATOR_REVIEW and draft.get("unresolved_operator_inputs"):
        final_state = OPERATOR_FINAL

    operator_summary = {
        "what_they_want": [li.get("description") or li.get("requested_item") for li in lines],
        "msrp": msrp.get("basket_msrp"),
        "historical_government_price": msrp.get("historical_government_extended")
        or (hist.get("median") * qty_total if hist.get("median") and qty_total else None),
        "estimated_acquisition_cost": acq_total,
        "recommended_bid": bid_total if scenarios.get("recommendation_status") == REC_BID else None,
        "recommendation_status": scenarios.get("recommendation_status"),
        "expected_profit": economics["ExpectedNetTransactionProfit"]["value"],
        "expected_margin": economics["NetTransactionMargin"],
        "pricing_confidence": acq_conf,
        "what_can_go_wrong": (sens or {}).get("cases", [])[1:] if sens else [],
        "what_must_verify": [a.get("target") for a in targets.get("actions") or []],
        "what_must_sign": ["authorized_signature", "required_certifications_if_applicable"],
        "what_still_missing": [u.get("field") for u in draft.get("unresolved_operator_inputs") or []],
        "when_due": (submission or {}).get("deadline"),
        "how_to_submit": (submission or {}).get("submission_method"),
        "timezone": (submission or {}).get("timezone"),
    }

    audit_entry = {
        "calculation_version": CALC_VERSION,
        "timestamp": now_utc().isoformat(),
        "solicitation_id": solicitation_id,
        "inputs": {
            "acquisition_total": acq_total,
            "acquisition_confidence": acq_conf,
            "freight": freight_cost,
            "financing": financing_cost,
            "expense": transaction_expense,
            "risk": risk,
            "historical_benchmark": {
                "median": hist.get("median"),
                "weighted_average": hist.get("weighted_average"),
                "recency_weighted": hist.get("recency_weighted_estimate"),
                "observations": hist.get("number_of_observations"),
            },
            "msrp_basket": msrp.get("basket_msrp"),
            "strategy": scenarios.get("recommendation_status"),
        },
        "resulting_price": bid_total,
        "resulting_profit": economics["ExpectedNetTransactionProfit"]["value"],
        "evidence_ids": [o.get("award_date") for o in (hist.get("provenance") or [])],
    }
    audit_trail = list(prior_audit or [])
    audit_trail.append(audit_entry)

    paid = []
    for q in paid_research_questions or []:
        paid.append({"question": q, "authorization": authorize_pricing_research(solicitation_id=solicitation_id, question=q)})

    return {
        "kind": "BidPricingAnalysis",
        "solicitation_id": solicitation_id,
        "transaction_economics": economics,
        "historical_benchmark": hist,
        "msrp_intelligence": msrp,
        "line_item_pricing": line_pricing,
        "pricing_scenarios": scenarios,
        "sensitivity": sens,
        "break_even": be,
        "commercial_verification_targets": targets,
        "price_freshness": price_fresh,
        "draft_bid_package": draft,
        "draft_validation": validation,
        "finalization_state": final_state,
        "operator_summary": operator_summary,
        "pricing_audit": audit_trail,
        "freight_state": freight_state,
        "financing_state": fin_state,
        "paid_research": paid,
        "outreach": {
            "operating_mode": get_operating_mode(),
            "emails_sent": 0,
            "calls_placed": 0,
            "quote_requests": 0,
            "registrations": 0,
            "financing_applications": 0,
            "signatures_applied": 0,
            "bids_submitted": 0,
        },
        "mode_snapshot": mode_snapshot(),
        "fabricated_profitable_economics": False,
        "win_probability_invented": False,
        "analyzed_at": now_utc().isoformat(),
        "calculation_version": CALC_VERSION,
    }


def reprice_after_change(
    analysis: dict[str, Any],
    *,
    change_type: str,
    updated_line_items: list[dict[str, Any]] | None = None,
    affected_line_ids: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    state = {
        "conclusions": {
            "acquisition_cost": analysis.get("transaction_economics", {}).get("ProductAcquisitionCost"),
            "freight": analysis.get("transaction_economics", {}).get("FreightCost"),
            "financing": analysis.get("transaction_economics", {}).get("FinancingCost"),
            "bid_price": (analysis.get("pricing_scenarios") or {}).get("recommended"),
            "profit": analysis.get("transaction_economics", {}).get("ExpectedNetTransactionProfit"),
            "unrelated_naics": {"ok": True},
            "line_item_pricing": analysis.get("line_item_pricing"),
            "extended_cost": True,
            "financing_timeline": True,
        },
        "lines": (analysis.get("line_item_pricing") or {}).get("lines"),
    }
    invalidated = invalidate_pricing_state(state, change_type=change_type, affected_line_ids=affected_line_ids)
    fresh = analyze_bid_pricing(
        solicitation_id=analysis["solicitation_id"],
        line_items=updated_line_items,
        prior_audit=analysis.get("pricing_audit"),
        **kwargs,
    )
    fresh["prior_invalidation"] = invalidated["last_invalidation"]
    return fresh
