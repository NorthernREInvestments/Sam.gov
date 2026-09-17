"""Commercial verification + execution control orchestrator."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from commercial_readiness_gate import (
    evaluate_commercial_readiness,
    evaluate_execution_gate,
    evaluate_pursuit_vs_execution,
)
from commercial_result_pipeline import (
    apply_financing_indication_to_economics,
    apply_supplier_quote_to_economics,
    compare_financing_offers,
    compare_supplier_quotes,
    ingest_verification_result,
    invalidate_commercial_evidence,
    negotiation_bands,
)
from commercial_verification_constants import (
    ACT_FINANCING_IND,
    ACT_SUPPLIER_QUOTE,
    ACT_VERIFY_AUTH,
    ACT_VERIFY_STOCK,
    AUTH_BINDING_QUOTE,
    AUTH_COND_FIN,
    FUND_EXHAUSTED,
    FUND_VERIFY_REQ,
    FUTURE_ACTION,
)
from commercial_verification_plan import build_commercial_verification_plan
from cost_governor import research_value_decision
from cost_governor_constants import TIER_1_ACTIVE
from external_action_control import (
    DryRunExecutionAdapter,
    execute_external_action,
    get_external_action_store,
)
from financing_verification import (
    assess_financing_compatibility,
    build_transaction_funding_requirement,
    evaluate_funding_gate,
    financing_verification_script,
    funding_path_coverage,
    load_financier_profiles_from_underwriting_artifact,
    record_financing_outcome,
)
from operating_mode import get_operating_mode, mode_snapshot
from pricing_scenarios import break_even_and_max_costs


def authorize_verification_spend(
    *,
    opportunity_id: str,
    question: str,
    could_change: bool = True,
    estimated_max_cost: float = 0.1,
) -> dict[str, Any]:
    from cost_governor import get_cost_governor

    voi = research_value_decision(
        question=question,
        could_change_pursuit_or_readiness=could_change,
    )
    if not voi.get("allow_paid"):
        return {"authorized": False, "reason": voi.get("reason"), "voi": voi}
    return get_cost_governor().authorize(
        {
            "provider": "sim",
            "action_type": "AI_COMPLETION",
            "deal_id": opportunity_id,
            "priority_tier": TIER_1_ACTIVE,
            "tracked": True,
            "question": question,
            "could_change_decision": could_change,
            "estimated_max_cost": estimated_max_cost,
            "idempotency_key": f"cver:{opportunity_id}:{question[:60]}",
        }
    )


def build_commercial_verification_bundle(
    *,
    opportunity_id: str,
    product_description: str | None = None,
    quantity: float | None = None,
    acquisition_estimate: float | None = None,
    acquisition_confidence: str = "UNKNOWN",
    freight_estimate: float | None = None,
    freight_confidence: str = "UNKNOWN",
    financing_estimate: float | None = None,
    bid_revenue: float | None = None,
    delivery_by: str | None = None,
    quote_valid_through: str | None = None,
    destination: str | None = None,
    authorization_required: bool = False,
    origin_required: bool = False,
    government_payment_terms: str | None = None,
    supplier_payment_timing: str | None = None,
    agency: str | None = None,
    supplier: str | None = None,
    operator_fico: int | None = None,
    operator_cash_available: float | None = 0.0,
    pg_acceptable: bool | None = True,
    economically_attractive: bool = False,
    stock_verified: bool = False,
    lead_time_verified: bool = False,
    unit_target: float | None = None,
    unit_ceiling: float | None = None,
    # Execution gate inputs (defaults conservative)
    solicitation_open: bool = True,
    deadline_viable: bool = True,
    package_fresh: bool = True,
    amendments_acknowledged: bool = False,
    compliance_passes: bool = False,
    product_compliant: bool = False,
    submission_method: str | None = None,
) -> dict[str, Any]:
    be = break_even_and_max_costs(
        bid_revenue=bid_revenue,
        freight=freight_estimate,
        financing=financing_estimate if financing_estimate is not None else 0.0,
        acquisition=acquisition_estimate,
    )
    max_acq = be.get("max_acquisition_for_target_profit")
    max_fin = be.get("max_financing")

    funding_req = build_transaction_funding_requirement(
        acquisition_cost=acquisition_estimate,
        freight=freight_estimate,
        supplier_payment_timing=supplier_payment_timing,
        government_payment_terms=government_payment_terms,
        maximum_financing_cost=max_fin,
        maximum_cash_contribution=operator_cash_available or 0.0,
        bid_revenue=bid_revenue,
        government_obligor=agency,
    )

    plan = build_commercial_verification_plan(
        opportunity_id=opportunity_id,
        acquisition_estimate=acquisition_estimate,
        acquisition_confidence=acquisition_confidence,
        max_acquisition=max_acq,
        target_acquisition=acquisition_estimate,
        freight_estimate=freight_estimate,
        freight_confidence=freight_confidence,
        financing_required=funding_req.get("funding_required"),
        financing_amount=funding_req.get("financing_amount_required"),
        max_financing_cost=max_fin,
        stock_verified=stock_verified,
        lead_time_verified=lead_time_verified,
        delivery_by=delivery_by,
        authorization_required=authorization_required,
        origin_required=origin_required,
        product_description=product_description,
        quantity=quantity,
        unit_target=unit_target or (
            (acquisition_estimate / quantity) if acquisition_estimate and quantity else None
        ),
        unit_ceiling=(max_acq / quantity) if max_acq and quantity else unit_ceiling,
        quote_valid_through=quote_valid_through,
        destination=destination,
    )

    profiles = load_financier_profiles_from_underwriting_artifact()
    compatibility = [
        assess_financing_compatibility(
            profile=p,
            funding_requirement=funding_req,
            operator_fico=operator_fico,
            operator_cash_available=operator_cash_available,
            pg_acceptable=pg_acceptable,
        )
        for p in profiles
    ]
    funding_gate = evaluate_funding_gate(
        funding_requirement=funding_req,
        compatibility=compatibility,
        economically_attractive=economically_attractive,
    )
    coverage = funding_gate.get("path_coverage") or funding_path_coverage(compatibility)
    fin_script = financing_verification_script(
        funding_requirement=funding_req,
        award_value=bid_revenue,
        agency=agency,
        supplier=supplier,
        delivery_period=delivery_by,
    )

    store = get_external_action_store()
    proposed_actions = []
    if plan.get("supplier_target"):
        st = plan["supplier_target"]
        proposed_actions.append(
            store.propose(
                opportunity_id=opportunity_id,
                action_type=ACT_SUPPLIER_QUOTE,
                purpose="Obtain binding supplier quote within hard ceiling",
                target=supplier or "supplier",
                information_to_send=st.get("script_summary"),
                information_requested=["unit_price", "availability", "lead_time", "quote_validity"],
                expected_cost=0.0,
            )
        )
        proposed_actions.append(
            store.propose(
                opportunity_id=opportunity_id,
                action_type=ACT_VERIFY_STOCK,
                purpose="Confirm stock/availability",
                target=supplier or "supplier",
                expected_cost=0.0,
            )
        )
    if authorization_required:
        proposed_actions.append(
            store.propose(
                opportunity_id=opportunity_id,
                action_type=ACT_VERIFY_AUTH,
                purpose="Obtain OEM/authorized channel documentation",
                target=supplier or "supplier",
            )
        )
    if funding_req.get("funding_required"):
        proposed_actions.append(
            store.propose(
                opportunity_id=opportunity_id,
                action_type=ACT_FINANCING_IND,
                purpose="Request transaction-specific financing indication",
                target="financier",
                information_to_send=fin_script.get("script"),
                information_requested=[
                    "advance",
                    "financing_cost",
                    "pg",
                    "credit_pull",
                    "minimum_fico",
                    "cash_contribution",
                ],
            )
        )

    readiness = evaluate_commercial_readiness(
        plan_built=True,
        actions_pending_auth=any(a.get("authorization_state") == "OPERATOR_REVIEW_REQUIRED" for a in proposed_actions),
        funding_gate_state=funding_gate.get("state"),
        economically_attractive=economically_attractive,
    )

    funding_feasible = funding_gate.get("state") in {
        "FUNDING_NOT_REQUIRED",
        "FUNDING_VERIFIED_FOR_TRANSACTION",
        "FUNDING_CONDITIONALLY_FEASIBLE",
    }
    funding_pending = funding_gate.get("state") in {
        FUND_VERIFY_REQ,
        "FUNDING_PATH_IDENTIFIED",
        "FUNDING_RESEARCH_ONLY",
        "FUNDING_REQUIREMENT_UNKNOWN",
    } or bool(funding_gate.get("economically_attractive_funding_verification_required"))

    execution = evaluate_execution_gate(
        solicitation_open=solicitation_open,
        deadline_viable=deadline_viable,
        package_fresh=package_fresh,
        amendments_acknowledged=amendments_acknowledged,
        compliance_passes=compliance_passes,
        product_compliant=product_compliant,
        acquisition_verified_current=acquisition_confidence in {"VERIFIED_BINDING", "VERIFIED_PUBLIC"},
        availability_sufficient=stock_verified,
        delivery_viable=lead_time_verified,
        freight_known=freight_estimate is not None and freight_confidence != "UNKNOWN",
        funding_required=bool(funding_req.get("funding_required")),
        funding_feasible=funding_feasible,
        funding_verification_pending=funding_pending and not funding_feasible,
        profit_floor_preserved=economically_attractive,
        profit_after_financing_ok=economically_attractive,
        required_forms_present=False,
        submission_instructions_known=bool(submission_method),
    )

    pursuit_exec = evaluate_pursuit_vs_execution(
        economically_attractive=economically_attractive,
        funding_gate_state=funding_gate.get("state"),
        transaction_funding_exhausted=bool(funding_gate.get("transaction_funding_exhausted")),
        execution_ready=execution.get("state") == "EXECUTION_READY_FOR_OPERATOR_FINALIZATION",
    )

    expected_profit = None
    if bid_revenue is not None and acquisition_estimate is not None:
        expected_profit = float(bid_revenue) - float(acquisition_estimate) - float(freight_estimate or 0) - float(
            financing_estimate or 0
        )

    dashboard = {
        "economics": {
            "current_expected_profit": expected_profit,
            "status": "ATTRACTIVE" if economically_attractive else "UNKNOWN",
            "profit_floor": funding_req.get("minimum_target_profit_after_financing"),
            "downside_room": max_acq,
        },
        "funding": {
            "amount_required": funding_req.get("financing_amount_required"),
            "maximum_acceptable_financing_cost": max_fin,
            "paths_identified": coverage.get("identified"),
            "paths_untested": coverage.get("untested"),
            "paths_verification_required": coverage.get("verification_required")
            + coverage.get("public_evidence_supports_possibility", 0)
            + coverage.get("untested", 0),
            "status": funding_gate.get("state"),
            "label": funding_gate.get("label"),
            "transaction_funding_exhausted": funding_gate.get("transaction_funding_exhausted"),
        },
        "supplier": {
            "current_estimate": acquisition_estimate,
            "target": plan.get("supplier_target", {}).get("bands", {}).get("TARGET") if plan.get("supplier_target") else None,
            "hard_ceiling": max_acq,
            "verification_status": "REQUIRED" if acquisition_confidence != "VERIFIED_BINDING" else "VERIFIED",
        },
        "financing": {
            "amount_required": funding_req.get("financing_amount_required"),
            "maximum_acceptable_financing_cost": max_fin,
            "potential_paths": [
                {"financier": c["financier"], "state": c["state"], "path_state": c.get("path_state")}
                for c in compatibility[:8]
            ],
            "pg": "see_profiles",
            "credit_pull": "see_profiles",
            "minimum_fico": "see_profiles",
            "personal_credit_dependency": "see_profiles",
            "cash_contribution": funding_req.get("maximum_cash_contribution"),
            "underwriting_model": "see_profiles",
            "verification_status": funding_gate.get("state"),
            "label": funding_gate.get("label"),
            "path_coverage": coverage,
        },
        "delivery": {
            "required_date": delivery_by,
            "supplier_commitment": "UNVERIFIED",
            "freight": freight_estimate,
            "status": "VERIFICATION_REQUIRED",
        },
        "pursuit": {
            "state": pursuit_exec.get("pursuit"),
            "alive": pursuit_exec.get("pursuit_alive"),
            "label": "KEEP PURSUING" if pursuit_exec.get("pursuit_alive") else "STOP",
        },
        "execution": {
            "state": execution.get("state"),
            "why_not_ready": execution.get("why_not_ready") or pursuit_exec.get("why_execution_not_ready"),
            "deal_cannot_be_done": pursuit_exec.get("deal_cannot_be_done"),
        },
        "actions": [
            {
                "action_id": a["action_id"],
                "type": a["action_type"],
                "authorization_state": a["authorization_state"],
                "proposed": FUTURE_ACTION,
            }
            for a in proposed_actions
        ],
    }

    return {
        "kind": "CommercialVerificationBundle",
        "opportunity_id": opportunity_id,
        "verification_plan": plan,
        "supplier_target": plan.get("supplier_target"),
        "negotiation_bands": negotiation_bands(target=acquisition_estimate, hard_ceiling=max_acq),
        "funding_requirement": funding_req,
        "financier_profiles": profiles,
        "financing_compatibility": compatibility,
        "funding_path_coverage": coverage,
        "funding_gate": funding_gate,
        "financing_script": fin_script,
        "external_actions": proposed_actions,
        "commercial_readiness": readiness,
        "execution_gate": execution,
        "pursuit_vs_execution": pursuit_exec,
        "dashboard": dashboard,
        "outreach": {
            "operating_mode": get_operating_mode(),
            "emails_sent": 0,
            "calls_placed": 0,
            "quote_requests": 0,
            "financing_applications": 0,
            "registrations": 0,
            "bids_submitted": 0,
        },
        "mode_snapshot": mode_snapshot(),
        "fabricated_financier_policies": False,
        "financing_learning_foundation": record_financing_outcome(
            financier="SCHEMA_ONLY",
            decision="NOT_APPLICABLE",
            government_customer=agency,
            transaction_size=funding_req.get("financing_amount_required"),
        ),
        "analyzed_at": now_utc().isoformat(),
    }


def process_verification_result_and_recalc(
    bundle: dict[str, Any],
    *,
    result: dict[str, Any],
    bid_revenue: float | None = None,
    freight: float | None = None,
    financing: float | None = None,
) -> dict[str, Any]:
    out = dict(bundle)
    rtype = result.get("result_type")
    payload = result.get("payload") or {}
    funding_req = bundle.get("funding_requirement") or {}

    if rtype == "supplier_quote":
        quoted = float(payload.get("extended_price") or payload.get("acquisition_cost") or 0)
        hard = (bundle.get("supplier_target") or {}).get("bands", {}).get("HARD_CEILING_LANDED")
        recalc = apply_supplier_quote_to_economics(
            bid_revenue=bid_revenue,
            quoted_acquisition=quoted,
            freight=freight if freight is not None else funding_req.get("freight"),
            financing=financing if financing is not None else 0.0,
            hard_ceiling=hard,
        )
        out["recalculation"] = recalc
        out["verification_outcome"] = recalc["verification_outcome"]
        out["updated_economics"] = recalc["economics"]
    elif rtype == "financing_indication":
        fin_cost = float(payload.get("expected_total_financing_cost") or payload.get("financing_cost") or 0)
        recalc = apply_financing_indication_to_economics(
            bid_revenue=bid_revenue,
            acquisition=float(funding_req.get("acquisition_cost") or 0),
            freight=freight if freight is not None else funding_req.get("freight"),
            financing_cost=fin_cost,
            max_financing_cost=funding_req.get("maximum_financing_cost"),
            min_fico_required=payload.get("minimum_fico"),
            operator_fico=payload.get("operator_fico"),
        )
        out["recalculation"] = recalc
        out["verification_outcome"] = recalc["verification_outcome"]
        out["updated_economics"] = recalc["economics"]
        if recalc["verification_outcome"] == "COMMERCIAL_VERIFICATION_PASSED":
            out["funding_gate"] = evaluate_funding_gate(
                funding_requirement=funding_req,
                compatibility=bundle.get("financing_compatibility"),
                indication_received=True,
                economically_attractive=True,
            )
    else:
        out["ingested_result"] = result

    out["result_ingested"] = result
    return out
