"""Funding Path Intelligence foundation — deterministic, zero paid APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from discovery.opportunity_gate import is_structurally_valid_opportunity
from funding_path_constants import (
    CONF_POST_AWARD_REQUIRED,
    CONF_PRELIMINARY_MATCH,
    CONF_SECURED,
    CONF_VERIFIED_PRE_BID,
    EV_INFERRED,
    EV_PROPOSED_AI,
    EV_STALE,
    EV_VERIFIED_BY_CALL,
    MATCH_MATCH,
    MATCH_NEEDS_VERIFICATION,
    MATCH_REJECT,
    PATH_PO_FINANCING,
    PATH_SUPPLIER_TERMS,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    READY_HARD_FAIL,
    READY_PLAUSIBLE,
    READY_PRE_BID,
    READY_UNKNOWN,
    STAGE_POST_AWARD,
    STAGE_PRE_BID,
    STRUCT_NEEDS_VERIFICATION,
)
from funding_path_intelligence import (
    apply_financing_cost_to_profit,
    assess_deal_funding_confidence,
    assess_government_contract_financing,
    build_funding_requirement,
    build_future_openai_decision_packet,
    build_operator_call_sheet,
    evaluate_funding_readiness,
    generate_hybrid_funding_paths,
    match_funding_source,
    record_contact_outcome,
    run_funding_path_workflow,
)
from funding_source_kb import (
    confirm_proposed_criterion,
    criterion_evidence,
    empty_funding_source_profile,
    fixture_compatible_po_lender,
    fixture_pg_required_lender,
    interpret_pg_marketing,
    mark_stale_criteria,
    production_knowledge_base_seed,
    propose_ai_criterion_update,
    set_criterion,
    unknown_criteria_count,
)


def _req(**kw):
    deadline_viability = kw.pop("deadline_viability", "GOOD")
    deadline_runway_days = kw.pop("deadline_runway_days", 10)
    cash = kw.pop("cash_required_before_delivery", 132000)
    company_is_new_entity = kw.pop("company_is_new_entity", True)
    is_first_government_contract = kw.pop("is_first_government_contract", True)
    funding_stage = kw.pop("funding_stage", STAGE_PRE_BID)
    deal_qualified = kw.pop("deal_qualified", True)
    base = dict(
        estimated_bid_value=185000,
        estimated_supplier_cost=132000,
        estimated_freight=3000,
        estimated_other_performance_cost=0,
    )
    base.update(kw)
    return build_funding_requirement(
        opportunity={
            "government_customer_type": "federal",
            "solicitation_number": "TEST-1",
            "agency": "Test Agency",
            "product_category": "equipment",
            "title": "Product resale equipment",
            "deadline_viability": deadline_viability,
            "deadline_runway_days": deadline_runway_days,
        },
        economics=base,
        deadline={
            "deadline_viability": deadline_viability,
            "deadline_runway_days": deadline_runway_days,
        },
        extras={
            "cash_required_before_delivery": cash,
            "company_is_new_entity": company_is_new_entity,
            "is_first_government_contract": is_first_government_contract,
            "funding_stage": funding_stage,
            "deal_qualified": deal_qualified,
            "supplier_can_ship_direct_to_government": True,
        },
    )


def test_pg_required_not_automatic_reject():
    r = match_funding_source(_req(), fixture_pg_required_lender())
    assert r["match_status"] != MATCH_REJECT
    assert "personal_guarantee_required" not in r["hard_fail_reasons"]
    assert r.get("operator_pg_review_required") is True or "pg_required_operator_review" in (
        r.get("positive_match_reasons") or []
    )
    assert r["funding_secured"] is False


def test_personal_cash_contribution_hard_reject():
    src = fixture_compatible_po_lender()
    src = set_criterion(
        src,
        "borrower_cash_contribution_required",
        True,
        criterion_evidence(
            criterion_name="borrower_cash_contribution_required",
            value=True,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    src["borrower_cash_contribution_required"] = True
    src["minimum_cash_contribution_pct"] = 20
    r = match_funding_source(_req(), src)
    assert r["match_status"] == MATCH_REJECT
    assert any(
        x in r["hard_fail_reasons"]
        for x in (
            "personal_cash_contribution_required",
            "verified_personal_cash_contribution_required_uncovered",
        )
    )


def test_personal_credit_dependency_hard_reject():
    src = fixture_compatible_po_lender()
    src = set_criterion(
        src,
        "personal_credit_checked",
        True,
        criterion_evidence(
            criterion_name="personal_credit_checked",
            value=True,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    src["personal_credit_checked"] = True
    src = set_criterion(
        src,
        "minimum_personal_fico",
        680,
        criterion_evidence(
            criterion_name="minimum_personal_fico",
            value=680,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    src["minimum_personal_fico"] = 680
    r = match_funding_source(_req(), src, deal_facts={"operator_personal_fico": 480})
    assert r["match_status"] == MATCH_REJECT
    assert any(
        x in r["hard_fail_reasons"]
        for x in ("personal_credit_dependency", "verified_minimum_fico_above_operator_score")
    )


def test_min_max_transaction_and_margin_rejects():
    src = fixture_compatible_po_lender()
    r_low = match_funding_source(_req(estimated_supplier_cost=1000, cash_required_before_delivery=1000), src)
    # rebuild requirement with low amount
    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 2000, "estimated_supplier_cost": 1000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 1000},
    )
    assert match_funding_source(req, src)["match_status"] == MATCH_REJECT
    assert "below_minimum_transaction" in match_funding_source(req, src)["hard_fail_reasons"]

    req_hi = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 9000000, "estimated_supplier_cost": 8000000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 8000000},
    )
    assert "above_maximum_transaction" in match_funding_source(req_hi, src)["hard_fail_reasons"]

    req_m = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 140000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 132000},
    )
    # margin ~5.7% < 8%
    assert "below_minimum_margin" in match_funding_source(req_m, src)["hard_fail_reasons"]


def test_first_contract_and_new_entity_prohibited():
    src = fixture_compatible_po_lender()
    src = set_criterion(
        src,
        "first_government_contract_allowed",
        False,
        criterion_evidence(
            criterion_name="first_government_contract_allowed",
            value=False,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    src["first_government_contract_allowed"] = False
    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 132000, "is_first_government_contract": True},
    )
    assert "first_government_contract_prohibited" in match_funding_source(req, src)["hard_fail_reasons"]

    src2 = fixture_compatible_po_lender()
    src2 = set_criterion(
        src2,
        "startup_allowed",
        False,
        criterion_evidence(
            criterion_name="startup_allowed",
            value=False,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    src2["startup_allowed"] = False
    src2["new_entity_allowed"] = False
    req2 = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 132000, "company_is_new_entity": True},
    )
    assert "new_entity_prohibited" in match_funding_source(req2, src2)["hard_fail_reasons"]


def test_unsupported_government_customer():
    src = fixture_compatible_po_lender()
    req = build_funding_requirement(
        opportunity={"government_customer_type": "international", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 132000},
    )
    assert "unsupported_government_customer" in match_funding_source(req, src)["hard_fail_reasons"]


def test_unknown_pg_and_cash_needs_verification_not_match():
    src = empty_funding_source_profile()
    src["source_name"] = "Unknown Lender"
    src["minimum_transaction"] = 10000
    r = match_funding_source(_req(), src)
    assert r["match_status"] == MATCH_NEEDS_VERIFICATION
    assert "personal_guarantee" in r["unknown_criteria"]
    assert "borrower_cash_contribution" in r["unknown_criteria"]
    assert r["funding_secured"] is False


def test_marketing_no_pg_options_is_conditional_not_not_required():
    ev = interpret_pg_marketing("No personal guarantee options available for qualified borrowers")
    assert ev["value"] == PG_CONDITIONAL
    assert ev["verification_status"] == EV_INFERRED
    assert ev["is_verified"] is False
    assert ev["value"] != PG_NOT_REQUIRED


def test_verified_compatible_source_can_match():
    r = match_funding_source(_req(), fixture_compatible_po_lender())
    assert r["match_status"] == MATCH_MATCH
    assert "hard_constraints_compatible" in r["positive_match_reasons"]
    assert r["funding_secured"] is False


def test_pre_bid_match_never_equals_funding_secured():
    m = match_funding_source(_req(), fixture_compatible_po_lender())
    conf = assess_deal_funding_confidence(requirement=_req(), match_results=[m])
    assert conf["funding_confidence"] == CONF_VERIFIED_PRE_BID
    assert conf["funding_secured"] is False
    assert conf["funding_confidence"] != CONF_SECURED


def test_post_award_workflow_differs():
    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={
            "cash_required_before_delivery": 132000,
            "funding_stage": STAGE_POST_AWARD,
            "award_received": True,
            "purchase_order_received": True,
        },
    )
    m = match_funding_source(req, fixture_compatible_po_lender())
    conf = assess_deal_funding_confidence(requirement=req, match_results=[m])
    assert conf["funding_confidence"] == CONF_POST_AWARD_REQUIRED
    assert conf["funding_secured"] is False
    ready = evaluate_funding_readiness(requirement=req, confidence=conf, match_results=[m])
    assert ready["readiness"] == "POST_AWARD_FUNDING_REQUIRED"


def test_call_sheet_skips_verified_asks_unknowns():
    src = fixture_compatible_po_lender()
    sheet = build_operator_call_sheet(requirement=_req(), source=src)
    keys = {q["key"] for q in sheet["questions"]}
    assert "personal_guarantee" not in keys  # verified current
    assert "cash_contribution" not in keys
    assert "opening" in sheet and "personal guarantee" in sheet["opening"].lower()
    assert sheet["OpenAI"] == 0

    unk = empty_funding_source_profile()
    unk["source_name"] = "Sparse"
    unk["general_phone"] = "555-9999"
    sheet2 = build_operator_call_sheet(requirement=_req(), source=unk)
    keys2 = {q["key"] for q in sheet2["questions"]}
    assert "personal_guarantee" in keys2
    assert "cash_contribution" in keys2


def test_stale_evidence_triggers_reverification():
    src = fixture_compatible_po_lender()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ev = src["criteria_evidence"]["personal_guarantee"]
    ev["reverification_due_at"] = past
    src["criteria_evidence"]["personal_guarantee"] = ev
    stale = mark_stale_criteria(src)
    assert stale["criteria_evidence"]["personal_guarantee"]["verification_status"] == EV_STALE
    sheet = build_operator_call_sheet(requirement=_req(), source=stale)
    keys = {q["key"] for q in sheet["questions"]}
    assert "personal_guarantee" in keys


def test_operator_notes_verbatim_and_ai_not_auto_verified():
    notes = (
        "Spoke with Sarah in underwriting. They finance federal/state/local product POs. "
        "Minimum $100k. They will fund up to 80% of supplier invoice. No FICO minimum, "
        "but require 20% borrower cash and a PG until three completed contracts."
    )
    outcome = record_contact_outcome(
        source_name="Example Lender",
        raw_operator_notes=notes,
        contact_name="Sarah",
        contact_title="Underwriting",
        ai_proposed_updates=[
            propose_ai_criterion_update(
                criterion_name="personal_guarantee",
                proposed_value=PG_REQUIRED,
                reasoning="Notes mention PG until three contracts",
            )
        ],
    )
    assert outcome["raw_operator_notes"] == notes
    assert outcome["ai_proposals_auto_verified"] is False
    assert outcome["ai_proposed_updates"][0]["requires_confirmation"] is True
    assert outcome["ai_proposed_updates"][0]["auto_verified"] is False

    proposed = propose_ai_criterion_update(
        criterion_name="personal_guarantee", proposed_value=PG_REQUIRED
    )
    assert proposed["proposed"]["verification_status"] == EV_PROPOSED_AI
    confirmed = confirm_proposed_criterion(
        empty_funding_source_profile(), proposed, actor="operator1", contact_name="Sarah"
    )
    assert confirmed["criteria_evidence"]["personal_guarantee"]["verification_status"] == EV_VERIFIED_BY_CALL


def test_supplier_financing_as_path_and_hybrids():
    req = _req()
    hybrids = generate_hybrid_funding_paths(req)
    assert any(h["path_type"] == PATH_SUPPLIER_TERMS or PATH_SUPPLIER_TERMS in str(h["components"]) for h in hybrids)
    partial = [h for h in hybrids if h.get("remaining_funding_gap") is not None]
    assert partial
    # Hybrid requiring personal cash fails
    fail_cash = generate_hybrid_funding_paths(
        req,
        components_sets=[[{"type": PATH_PO_FINANCING, "covers": 120000, "pg": False, "cash": 10000}]],
    )[0]
    assert fail_cash["status"] == "FAIL_HARD_CONSTRAINT"
    assert "personal_cash_contribution_required" in fail_cash["hard_fail_reasons"]
    fail_pg = generate_hybrid_funding_paths(
        req,
        components_sets=[[{"type": PATH_PO_FINANCING, "covers": 132000, "pg": True, "cash": 0}]],
    )[0]
    assert fail_pg["status"] == "OPERATOR_PG_REVIEW_REQUIRED"
    assert fail_pg.get("operator_pg_review_required") is True
    assert "personal_guarantee_required" not in fail_pg["hard_fail_reasons"]


def test_financing_cost_reduces_profit_and_can_fail_10k():
    r = apply_financing_cost_to_profit(
        profit_before_financing=14000,
        financing_fee_estimate=6000,
        bid_value=100000,
    )
    assert r["profit_after_financing"] == 8000
    assert r["meets_minimum_profit_target"] is False
    assert r["fails_minimum_profit_due_to_financing"] is True

    unk = apply_financing_cost_to_profit(profit_before_financing=14000)
    assert unk["funding_cost_confidence"] == "UNKNOWN"
    assert unk["estimated_total_funding_cost"] is None
    assert unk["meets_minimum_profit_target"] is None


def test_government_financing_absent_unknown_not_available():
    g = assess_government_contract_financing()
    assert g["availability"] == STRUCT_NEEDS_VERIFICATION
    assert g["government_financing_verified"] is False
    assert g["contracting_officer_verification_needed"] is True


def test_deadline_gates_and_too_late_not_rescued_by_funding():
    from discovery.deadline_viability import may_trigger_paid_research

    assert may_trigger_paid_research("TOO_LATE", research_kind="financing")["allowed"] is False
    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        deadline={"deadline_viability": "TOO_LATE", "deadline_runway_days": 1},
        extras={"cash_required_before_delivery": 132000, "deal_qualified": True},
    )
    m = match_funding_source(req, fixture_compatible_po_lender())
    conf = assess_deal_funding_confidence(requirement=req, match_results=[m])
    assert conf["reason"] == "deadline_too_late"
    assert conf["funding_secured"] is False


def test_failed_deal_qualification_not_overridden():
    req = build_funding_requirement(
        opportunity={"government_customer_type": "federal", "product_category": "equipment"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 0, "estimated_other_performance_cost": 0},
        extras={"cash_required_before_delivery": 132000, "deal_qualified": False},
    )
    m = match_funding_source(req, fixture_compatible_po_lender())
    conf = assess_deal_funding_confidence(requirement=req, match_results=[m])
    assert "qualification" in conf["reason"]
    ready = evaluate_funding_readiness(requirement=req, confidence=conf, match_results=[m])
    assert ready["readiness"] == READY_HARD_FAIL


def test_readiness_plausible_and_unknown():
    m = match_funding_source(_req(), empty_funding_source_profile())
    conf = assess_deal_funding_confidence(requirement=_req(), match_results=[m])
    assert conf["funding_confidence"] in {
        "NEEDS_LENDER_VERIFICATION",
        CONF_PRELIMINARY_MATCH,
        "THEORETICAL_PATH_ONLY",
    }
    ready = evaluate_funding_readiness(requirement=_req(), confidence=conf, match_results=[m])
    assert ready["readiness"] in {READY_PLAUSIBLE, READY_UNKNOWN}

    m2 = match_funding_source(_req(), fixture_compatible_po_lender())
    conf2 = assess_deal_funding_confidence(requirement=_req(), match_results=[m2])
    ready2 = evaluate_funding_readiness(requirement=_req(), confidence=conf2, match_results=[m2])
    assert ready2["readiness"] == READY_PRE_BID


def test_workflow_and_openai_packet_zero_apis():
    wf = run_funding_path_workflow(
        opportunity={"government_customer_type": "federal", "product_category": "equipment", "title": "Switches"},
        economics={"estimated_bid_value": 185000, "estimated_supplier_cost": 132000, "estimated_freight": 3000},
        deadline={"deadline_viability": "GOOD", "deadline_runway_days": 10},
        sources=[fixture_compatible_po_lender()],
        extras={"cash_required_before_delivery": 132000, "company_is_new_entity": True, "is_first_government_contract": True},
    )
    assert wf["OpenAI"] == 0 and wf["SAM"] == 0 and wf["USAspending"] == 0 and wf["paid"] == 0
    assert wf["call_sheets"]
    pkt = build_future_openai_decision_packet(
        requirement=wf["requirement"],
        sources=[fixture_compatible_po_lender()],
        match_results=wf["matches"],
    )
    assert pkt["openai_requests"] == 0
    assert pkt["OpenAI"] == 0
    assert "hard_constraints_authoritative" in pkt["rules"]


def test_production_kb_seeded_with_evidence():
    kb = production_knowledge_base_seed()
    assert kb["OpenAI"] == 0
    assert len(kb.get("programs") or kb.get("private_lenders") or []) >= 15
    # FAR note remains contextual
    assert kb["government_program_notes"][0]["applies_to_specific_solicitation"] is False
    # Ranking never means secured
    for p in kb.get("programs") or []:
        assert p.get("funding_secured") is False
        assert p.get("approved") is False


def test_structural_gate_still_green():
    gate = is_structurally_valid_opportunity(
        {
            "title": "Network Switches Equipment Purchase RFP",
            "solicitation_number": "IFB-26-100",
            "external_id": "x1",
            "deadline_raw": "12/01/2026",
            "detail_url": "https://example.test/1",
            "agency": "State Agency",
            "status": "OPEN",
        }
    )
    assert gate["valid"] is True


def test_fixture_unknown_criteria_count_low():
    assert unknown_criteria_count(fixture_compatible_po_lender()) == 0
    assert unknown_criteria_count(empty_funding_source_profile()) > 5
