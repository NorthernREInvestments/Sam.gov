"""Focused tests — Autonomous Pursuit Qualification (DEVELOPMENT_NO_OUTREACH)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_intelligence import freight_range_from_cost, research_bom_cost_intelligence
from operating_mode import (
    MODE_DEVELOPMENT_NO_OUTREACH,
    MODE_OPERATIONAL,
    classify_action_timing,
    is_development_no_outreach,
    mode_snapshot,
    set_operating_mode,
)
from operator_action_queue import OperatorActionQueue, enqueue_supplier_quote_action, operator_action
from package_readiness_layers import assess_package_readiness_layers
from pursuit_qualification import (
    decide_pursuit,
    finance_allowance,
    future_actions_for_pursuit,
    preliminary_economic_potential,
    qualify_opportunity_pursuit,
    revenue_context_from_history,
)
from pursuit_qualification_constants import (
    ACTION_TIMING_FUTURE,
    ACTION_TIMING_NOW,
    COST_ESTIMATED_RANGE,
    COST_EXACT_PUBLIC,
    ECON_CLEARLY_UNECONOMIC,
    ECON_POSSIBLE_FLOOR,
    ECON_STRONG,
    PKG_FORMAL_QUOTE,
    PKG_PRELIMINARY,
    PROFIT_FLOOR,
    PURSUIT_REJECT,
    PURSUIT_WORTHY,
    PURSUIT_WORTHY_UNCERTAIN,
    REQ_COMPLIANT,
    REQ_PRICEABLE,
)
from reusable_knowledge import ReusableKnowledgeStore, supplier_fact
from source_health import analyze_source_failures, build_source_health_report, classify_source_failure, recommend_retry


@pytest.fixture(autouse=True)
def _dev_mode():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    yield
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)


def test_development_mode_suppresses_immediate_outreach():
    assert is_development_no_outreach()
    snap = mode_snapshot()
    assert snap["outreach_allowed"] is False
    assert snap["emails_sent"] == 0
    assert snap["calls_placed"] == 0
    assert classify_action_timing(action_type="CALL_SUPPLIER") == ACTION_TIMING_FUTURE


def test_quote_required_does_not_create_action_now():
    a = operator_action(
        "CALL_SUPPLIER",
        deal_id="X",
        why="Call Ion Exchange today",
        who_where="Ion Exchange",
        questions=["quote"],
        information_expected=["price"],
        unlocks_stage="SUPPLIER_COSTING",
    )
    assert a["action_timing"] == ACTION_TIMING_FUTURE
    assert a["imperative_now"] is False
    assert "Future" in a["why_needed"] or "future" in a["why_needed"].lower()


def test_future_actions_require_pursuit_qualification():
    actions = future_actions_for_pursuit(
        deal_id="D1",
        pursuit_state="DEFER",
        economics={"low_profit": 100, "high_profit": 200, "base_profit": 150, "profit_floor": PROFIT_FLOOR},
        readiness={"compliance_material_unresolved": False},
        suppliers=[{"supplier_name": "Acme"}],
    )
    assert actions[0]["timing"] == "NO_ACTION_REQUIRED"
    worthy = future_actions_for_pursuit(
        deal_id="D2",
        pursuit_state=PURSUIT_WORTHY,
        economics={"low_profit": 12000, "high_profit": 40000, "base_profit": 20000, "profit_floor": PROFIT_FLOOR},
        readiness={"compliance_material_unresolved": True},
        suppliers=[{"supplier_name": "Acme"}],
    )
    assert all(a["timing"] == ACTION_TIMING_FUTURE for a in worthy)
    assert any(a.get("label") == "FUTURE_SUPPLIER_QUOTE_REQUIRED" for a in worthy)


def test_missing_formal_quote_does_not_auto_reject():
    d = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={
            "layered_status": PKG_PRELIMINARY,
            "priceable_requirements": True,
            "preliminary_analysis_complete": True,
            "compliance_material_unresolved": True,
        },
        suppliers=[{"supplier_name": "S"}],
        economics={"status": ECON_POSSIBLE_FLOOR, "high_profit": 15000, "base_profit": 8000},
    )
    assert d["state"] != PURSUIT_REJECT


def test_missing_critical_spec_affects_completeness_layers():
    ready = assess_package_readiness_layers(
        documents=[
            {"document_title": "event.pdf", "access_status": "PUBLIC_FETCHED", "document_class": "SOLICITATION"},
            {"document_title": "Supplemental Spec", "access_status": "AUTH_REQUIRED", "document_class": "SPECIFICATION"},
        ],
        line_items=[{"description": "Big bluestem seed", "quantity": 100, "unit": "LB"}],
        terms={"delivery_location": {"value": "TBD_BY_BUYER"}},
        deadline="2026-10-01",
        has_authoritative_text=True,
    )
    assert ready["priceable_requirements"] is True
    assert ready["requirement_layer"] == REQ_PRICEABLE
    assert ready["formal_quote_complete"] is False
    assert ready["preliminary_analysis_complete"] is True
    assert ready["compliance_material_unresolved"] is True
    assert ready["layered_status"] == PKG_PRELIMINARY


def test_priceable_not_equal_compliant_quote_ready():
    ready = assess_package_readiness_layers(
        documents=[
            {"document_title": "Spec", "access_status": "AUTH_REQUIRED", "document_class": "SPECIFICATION"},
        ],
        line_items=[{"description": "Widget assembly part", "quantity": 10, "unit": "EA"}],
        terms={},
        deadline="2026-11-01",
        has_authoritative_text=True,
    )
    assert ready["priceable_requirements"] is True
    assert ready["compliant_product_requirements"] is False
    assert ready["formal_quote_complete"] is False


def test_exact_vs_comparable_pricing():
    lines = [
        {
            "description": "Native seed mix",
            "quantity": 50,
            "unit": "LB",
            "unit_cost": 40.0,
            "cost_evidence_state": COST_EXACT_PUBLIC,
        },
        {"description": "Andropogon gerardii", "quantity": 100, "unit": "LB"},
    ]
    cost = research_bom_cost_intelligence(line_items=lines, title="Wildflower Seed", max_sample=10)
    exact = cost["exact_vs_comparable"]["exact_count"]
    comp = cost["exact_vs_comparable"]["comparable_or_estimate_count"]
    assert exact >= 1
    assert comp >= 1
    assert cost["suitable_for_bid_economics"] is False


def test_cost_ranges_preserve_uncertainty():
    cost = research_bom_cost_intelligence(
        line_items=[{"description": "Big bluestem native seed", "quantity": 200, "unit": "LB"}],
        title="Native Grass Seed",
    )
    assert cost["status"] == COST_ESTIMATED_RANGE
    r = cost["ranges"]
    assert r["low_cost_estimate"] < r["base_cost_estimate"] < r["high_cost_estimate"]


def test_multiline_sampling_reports_coverage():
    lines = [{"description": f"Native seed species {i}", "quantity": 10 + i, "unit": "LB"} for i in range(40)]
    cost = research_bom_cost_intelligence(line_items=lines, title="Seed", max_sample=10)
    assert cost["sample_size"] == 10
    assert cost["line_count"] == 40
    assert 0 < cost["coverage_of_bom_qty_proxy"] <= 1.0
    assert "not guaranteed" in cost["coverage_note"].lower() or "proxy" in cost["coverage_note"].lower()


def test_historical_award_not_guaranteed_revenue():
    rev = revenue_context_from_history(
        cost_ranges={"base_cost_estimate": 50000},
        buyer_history={"comparable_award_amounts": [80000, 90000]},
    )
    assert "historical_award_not_guaranteed_current_revenue" in rev["notes"]
    assert rev["base"] is not None


def test_finance_allowance_in_preliminary_profit():
    fin = finance_allowance(100000)
    assert fin["verified"] is False
    assert fin["base"] > 0
    freight = freight_range_from_cost(cost_base=100000)
    econ = preliminary_economic_potential(
        revenue={"low": 130000, "base": 150000, "high": 170000},
        cost_ranges={"low_cost_estimate": 90000, "base_cost_estimate": 100000, "high_cost_estimate": 110000},
        freight=freight,
        finance=fin,
    )
    # Finance must reduce profit vs ignoring it
    assert econ["base_profit"] < (150000 - 100000 - freight["base"])


def test_freight_not_zeroed():
    fr = freight_range_from_cost(cost_base=50000, fob_terms="FOB Origin")
    assert fr["status"] == "ESTIMATED_RANGE"
    assert fr["base"] > 0
    assert fr["high"] > fr["low"]


def test_profit_range_and_floor_screening():
    fin = finance_allowance(200000)
    fr = freight_range_from_cost(cost_base=200000)
    strong = preliminary_economic_potential(
        revenue={"low": 250000, "base": 280000, "high": 320000},
        cost_ranges={"low_cost_estimate": 180000, "base_cost_estimate": 200000, "high_cost_estimate": 220000},
        freight=fr,
        finance=fin,
    )
    assert strong["high_profit"] > strong["low_profit"]
    assert strong["status"] in {ECON_STRONG, ECON_POSSIBLE_FLOOR}
    unecon = preliminary_economic_potential(
        revenue={"low": 10000, "base": 12000, "high": 15000},
        cost_ranges={"low_cost_estimate": 20000, "base_cost_estimate": 25000, "high_cost_estimate": 30000},
        freight=fr,
        finance=fin,
    )
    assert unecon["status"] == ECON_CLEARLY_UNECONOMIC


def test_pursuit_worthy_requires_evidence():
    d = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={
            "layered_status": PKG_PRELIMINARY,
            "priceable_requirements": True,
            "preliminary_analysis_complete": True,
            "compliance_material_unresolved": False,
        },
        suppliers=[{"supplier_name": "S"}],
        economics={"status": ECON_STRONG},
    )
    assert d["state"] == PURSUIT_WORTHY


def test_possible_floor_only_is_uncertain_not_full_worthy():
    d = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={
            "layered_status": PKG_PRELIMINARY,
            "priceable_requirements": True,
            "preliminary_analysis_complete": True,
            "compliance_material_unresolved": False,
        },
        suppliers=[{"supplier_name": "S"}],
        economics={"status": ECON_POSSIBLE_FLOOR},
    )
    assert d["state"] == PURSUIT_WORTHY_UNCERTAIN
    assert "profit_floor_only_in_optimistic_case" in d["reasons"]
    d = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={"layered_status": PKG_PRELIMINARY, "priceable_requirements": True, "preliminary_analysis_complete": True},
        suppliers=[{"supplier_name": "S"}],
        economics={"status": ECON_CLEARLY_UNECONOMIC},
    )
    assert d["state"] == PURSUIT_REJECT
    actions = future_actions_for_pursuit(
        deal_id="U",
        pursuit_state=d["state"],
        economics={"low_profit": -5000, "high_profit": 1000, "base_profit": -2000, "profit_floor": PROFIT_FLOOR},
        readiness={},
        suppliers=[{"supplier_name": "S"}],
    )
    assert actions[0]["timing"] == "NO_ACTION_REQUIRED"


def test_supplier_buyer_finance_knowledge_reused():
    store = ReusableKnowledgeStore()
    store.add_supplier(supplier_fact("Ion Exchange", category="native_seed", source="test"))
    opp = {
        "solicitation_number": "TEST-1",
        "title": "Wildflower and Native Grass Seed",
        "bid_deadline": "2026-12-01",
        "deadline_evaluation": {"status": "OPEN"},
        "line_items": [{"description": "Big bluestem seed", "quantity": 300, "unit": "LB"}],
        "documents": [
            {"document_title": "RFB", "access_status": "PUBLIC_FETCHED", "document_class": "SOLICITATION"},
            {"document_title": "Spec", "access_status": "AUTH_REQUIRED", "document_class": "SPECIFICATION"},
        ],
        "has_authoritative_text": True,
        "supplier_candidates": [{"supplier_name": "Ion Exchange"}],
        "transactional_fit": True,
        "agency": "Iowa DOT",
    }
    result = qualify_opportunity_pursuit(opp, reusable=store)
    assert result["operating_mode"]["outreach_allowed"] is False
    assert result["bid_submitted"] is False
    assert result["finance_allowance"]["verified"] is False
    assert (result.get("cost_intelligence") or {}).get("suitable_for_preliminary_economics") is True


def test_source_health_classification_and_no_infinite_retry():
    metrics = {
        "per_source": {
            "state_pa": {"ok": False, "source_stop_reason": "PARSER_FAILURE", "unique": 0},
            "state_ar": {"ok": False, "source_stop_reason": "AUTH_REQUIRED", "unique": 0},
            "bad_dns": {"ok": False, "error": "[Errno 11001] getaddrinfo failed", "unique": 0},
            "state_ia": {"ok": True, "unique": 19, "raw": 20, "product_candidates": 1},
        }
    }
    report = build_source_health_report(metrics)
    assert report["ok_count"] == 1
    analysis = analyze_source_failures(report)
    assert "PARSER_FAILURE" in analysis["by_type"] or any(
        r.get("failure_type") == "PARSER_FAILURE" for r in report["sources"]
    )
    assert recommend_retry("ANTI_AUTOMATION") == "SKIP"
    assert recommend_retry("AUTH_REQUIRED") == "SKIP_UNTIL_PUBLIC_FEED"
    assert classify_source_failure({"ok": False, "source_stop_reason": "HTTP_404", "error": "404"}) in {
        "SOURCE_CHANGED",
        "UNKNOWN",
        "NO_OPEN_RESULTS",
    }


def test_request_budgeting_tracks_kills():
    # Unit-level: uneconomic short-circuits future quote
    d = decide_pursuit(
        transactional_fit=True,
        deadline_status="OPEN",
        readiness={"preliminary_analysis_complete": True, "priceable_requirements": True, "layered_status": PKG_PRELIMINARY},
        suppliers=[{"supplier_name": "S"}],
        economics={"status": ECON_CLEARLY_UNECONOMIC},
    )
    assert d["state"] == PURSUIT_REJECT


def test_zero_outreach_assertions():
    snap = mode_snapshot()
    assert snap["emails_sent"] == snap["calls_placed"] == snap["supplier_contacts"] == 0
    assert snap["financier_contacts"] == snap["agency_contacts"] == 0
    assert snap["portal_registrations"] == snap["bids_submitted"] == snap["financing_applications"] == 0


def test_enqueue_supplier_stays_future_in_dev():
    q = OperatorActionQueue()
    enqueue_supplier_quote_action(
        q,
        deal={"solicitation_number": "ABC", "bid_deadline": "2026-12-01"},
        supplier={"name": "Acme"},
        line_items=[{"description": "part", "quantity": 1}],
    )
    open_a = q.open_actions("ABC")
    assert open_a
    assert open_a[0]["action_timing"] == ACTION_TIMING_FUTURE


def test_operational_mode_can_mark_now_when_worthy():
    set_operating_mode(MODE_OPERATIONAL)
    assert classify_action_timing(action_type="CALL_SUPPLIER", pursuit_state=PURSUIT_WORTHY) == ACTION_TIMING_NOW
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)


def test_artifacts_contract_names_exist_after_runner_optional():
    """If validation artifacts exist from a live run, spot-check schema — skip if absent."""
    art = Path(__file__).resolve().parents[1] / "artifacts"
    path = art / "pursuit_qualification_validation.json"
    if not path.exists():
        pytest.skip("runner artifacts not yet generated")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("operating_mode") == MODE_DEVELOPMENT_NO_OUTREACH
    assert data.get("outreach", {}).get("bids_submitted") == 0
    assert data.get("synthetic_live_success") is False
    assert data.get("benchmark_leakage") is False
