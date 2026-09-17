"""Execution intelligence — capital, financing fit, execution score, ranking."""

from __future__ import annotations

from m3_commercial_engine import SCORE_HIGH, SCORE_LOW
from m3_execution_intelligence import (
    BUCKET_A,
    BUCKET_B,
    BUCKET_C,
    BUCKET_D,
    CAP_HIGH,
    CAP_LOW,
    FIT_GOOD,
    FIT_POOR,
    RISK_HIGH,
    RISK_LOW,
    analyze_execution_top_opportunities,
    build_capital_requirement,
    build_contract_risk,
    build_delivery_complexity,
    build_execution_intelligence,
    build_financing_fit,
    build_transaction_profile,
    deal_room_execution_section,
    score_execution,
)
from m3_pipeline_store import M3PipelineStore


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "ex-1"),
        "title": "Dell Storage",
        "agency": "State of Illinois",
        "estimated_value": 75000,
        "product_category": "IT_STORAGE",
        "supplier_intelligence": {
            "Product": {"Technical_description": "Dell Storage", "sufficient_for_pricing_research": True},
            "Supply_chain": {
                "supplier_confidence": SCORE_HIGH,
                "all_channels": [{"company": "Dell"}, {"company": "CDW"}, {"company": "SHI"}],
            },
            "Pricing_evidence": {"primary_level": "LEVEL_4_UNKNOWN"},
            "ACQUISITION_COST_CONFIDENCE": "UNKNOWN",
        },
        "commercial_intelligence": {"COMMERCIAL_OPPORTUNITY_SCORE": "MEDIUM", "Supply_Confidence": SCORE_HIGH},
        "competitive_intelligence": {
            "FIRST_DEAL": {"FIRST_DEAL_SCORE": 75},
            "FINANCEABILITY": {"classification": "EASY_TO_FINANCE"},
            "COMPETITION_PROFILE": {"profile": "UNKNOWN"},
            "CATEGORY": "IT_EQUIPMENT",
        },
    }
    base.update(kwargs)
    return base


def test_cash_gap_unknown_when_cost_unknown():
    txn = build_transaction_profile(_opp())
    assert txn["estimated_product_cost_status"] == "UNKNOWN"
    assert txn["required_upfront_capital"] == "UNKNOWN"
    assert "UNKNOWN" in str(txn["working_capital_gap"]) or txn["working_capital_gap"] == "UNKNOWN_FULL_CONTRACT_MAY_NEED_BRIDGING"
    cap = build_capital_requirement(_opp(), txn)
    assert "financing_existence_not_assumed" in cap["notes"]


def test_cash_gap_with_known_cost():
    row = _opp(
        supplier_intelligence={
            "Product": {"sufficient_for_pricing_research": True},
            "Supply_chain": {"supplier_confidence": SCORE_HIGH, "all_channels": [{}, {}, {}]},
            "Pricing_evidence": {"primary_level": "LEVEL_2_PUBLIC_COMMERCIAL"},
            "ACQUISITION_COST_CONFIDENCE": SCORE_HIGH,
            "cost_detail": {"best_price": 40000},
        }
    )
    txn = build_transaction_profile(row)
    assert txn["estimated_product_cost_status"] == "KNOWN"
    assert txn["required_upfront_capital"] == 40000
    assert txn["working_capital_gap"] == 40000.0
    cap = build_capital_requirement(row, txn)
    assert cap["classification"] == CAP_LOW


def test_financing_classification():
    row = _opp()
    txn = build_transaction_profile(row)
    cap = build_capital_requirement(row, txn)
    fin = build_financing_fit(row, txn, cap)
    assert fin["classification"] == FIT_GOOD
    poor = build_financing_fit(
        _opp(title="Custom classified fabrication services", supplier_intelligence={}),
        build_transaction_profile(_opp(title="Custom classified fabrication services")),
        {"classification": CAP_HIGH},
    )
    assert poor["classification"] == FIT_POOR


def test_execution_and_risk_scoring():
    row = _opp()
    pkg = build_execution_intelligence(row)
    assert pkg["EXECUTION"]["band"] in {SCORE_HIGH, "MEDIUM", SCORE_LOW}
    assert pkg["CONTRACT_RISK"]["classification"] == RISK_LOW
    hard = build_execution_intelligence(
        _opp(
            title="Custom installation repair services",
            estimated_value=2_000_000,
            supplier_intelligence={},
            commercial_intelligence={"COMMERCIAL_OPPORTUNITY_SCORE": "LOW"},
            competitive_intelligence={"FINANCEABILITY": {"classification": "DIFFICULT"}},
        )
    )
    assert hard["DELIVERY"]["band"] in {RISK_HIGH, "MEDIUM"}
    assert hard["BUCKET"] in {BUCKET_D, BUCKET_C, BUCKET_B}


def test_opportunity_ranking_and_deal_room(tmp_path):
    store = M3PipelineStore(path=tmp_path / "p.json", durable=False)
    store._rows["a"] = _opp(canonical_id="a")
    store._rows["b"] = _opp(
        canonical_id="b",
        title="Window Washing Services",
        estimated_value=20000,
        supplier_intelligence={},
        commercial_intelligence={"COMMERCIAL_OPPORTUNITY_SCORE": "LOW"},
        competitive_intelligence={"FIRST_DEAL": {"FIRST_DEAL_SCORE": 10}},
    )
    store.save()
    out = analyze_execution_top_opportunities(store, limit=5)
    assert out["analyzed"] >= 1
    assert out["paid"] == 0
    assert "READY_TO_PURSUE" in out["buckets"]
    assert out["TOP_FIRST_TRANSACTIONS"]
    sec = deal_room_execution_section(store.get("a") or _opp(canonical_id="a"))
    assert sec["kind"] == "M3DealRoomExecutionIntelligence"
    assert sec["Recommended_Path"]
    assert "Execution_Score" in sec


def test_delivery_prefers_ship_and_deliver():
    easy = build_delivery_complexity(_opp(title="Cisco Systems Network Switches"))
    hard = build_delivery_complexity(_opp(title="Elevator installation and repair services"))
    assert easy["DELIVERY_COMPLEXITY_SCORE"] < hard["DELIVERY_COMPLEXITY_SCORE"]
    assert build_contract_risk(_opp(title="Cisco Systems Network Switches"))["classification"] == RISK_LOW
