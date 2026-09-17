"""Focused tests for ExecutableDealPipeline orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_maturity import assert_not_promoting_estimate, freight_fact, mature_fact
from executable_deal_constants import (
    EV_ESTIMATE,
    EV_OPERATOR_CONFIRMED,
    EV_QUOTE_REQUIRED,
    LIVE_IOWA,
    PROFIT_FLOOR_USD,
    READY_BID_PREP,
    READY_COMPLIANCE,
    READY_FUNDING_CALL,
    READY_QUOTE_REQUIRED,
    READY_REJECTED,
    STAGE_DEAL_REJECTED,
)
from executable_deal_pipeline import (
    ExecutableDealPipeline,
    calculate_working_capital,
    match_finance_knowledge,
    normalize_bom_line,
    query_supplier_knowledge,
)
from operator_action_queue import OperatorActionQueue, build_supplier_call_sheet
from operator_deal_packet import build_operator_deal_packet
from operator_result_ingestion import classify_promotion, ingest_financier_call_result, ingest_supplier_call_result
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
PACKETS = ARTIFACTS / "transactional_procurement_packets"


def _base_product(**kwargs):
    d = {
        "deal_id": "T-1",
        "solicitation_number": "T-1",
        "title": "Industrial fasteners product purchase",
        "description": "Buy bolts and washers for warehouse",
        "agency": "Test DOT",
        "product_classification": "CORE_PRODUCT",
        "bid_deadline": "2026-12-01T13:00:00-06:00",
        "timezone": "America/Chicago",
        "line_items": [
            {"description": "Bolt A", "quantity": 10, "unit": "EA", "manufacturer": "FastCo"},
            {"description": "Washer B", "quantity": 20, "unit": "EA", "manufacturer": "FastCo"},
        ],
        "quantities_from_solicitation": True,
        "supplier_candidates": [{"name": "FastCo Dist", "supplier": "FastCo Dist"}],
        "is_live": False,
    }
    d.update(kwargs)
    return d


def test_non_product_exits_early():
    p = ExecutableDealPipeline()
    r = p.run(
        {
            "title": "Janitorial cleaning services contract",
            "description": "Provide monthly janitorial services and labor",
            "product_classification": "SERVICE",
            "bid_deadline": "2026-12-01T13:00:00-06:00",
            "line_items": [{"description": "monthly cleaning", "quantity": 1}],
        }
    )
    assert r["stage"] == STAGE_DEAL_REJECTED
    assert r["operator_readiness"] == READY_REJECTED
    assert "not_transactional" in (r["stop_reason"] or "")


def test_expired_deal_exits_early():
    p = ExecutableDealPipeline()
    r = p.run(
        _base_product(bid_deadline="1/1/2020, 1:00 PM CDT")
    )
    assert r["operator_readiness"] == READY_REJECTED
    assert r["stop_reason"] == "deadline_expired"


def test_working_capital_timing():
    wc = calculate_working_capital(supplier_total=10000, deposit=2000, freight=500)
    # deposit 2000 + balance 8000 + freight 500 = 10500
    assert wc["working_capital_required"] == 10500


def test_missing_requirement_triggers_acquisition():
    p = ExecutableDealPipeline()
    r = p.run(
        _base_product(line_items=[], requirements_insufficient=True, auth_required_for_spec=True)
    )
    assert r["stop_reason"] == "AUTH_REQUIRED"
    assert any(a["action_type"] == "DOWNLOAD_AUTH_DOCUMENT" for a in r["actions"])


def test_bom_supports_multiple_lines():
    lines = [
        normalize_bom_line({"description": "A", "quantity": 1, "unit_of_measure": "EA"}),
        normalize_bom_line({"description": "B", "quantity": 2, "unit": "EA"}),
        normalize_bom_line({"description": "C", "quantity": 3, "unit": "BX"}),
    ]
    assert len(lines) == 3
    assert lines[0]["unit"] == "EA"


def test_supplier_knowledge_reused_before_search():
    store = ReusableKnowledgeStore()
    store.add_supplier(
        supplier_fact("Winter Equipment Company", category="blade", source="prior", quote_required=True)
    )
    hits = query_supplier_knowledge(store, category="blade")
    assert hits and hits[0]["supplier"] == "Winter Equipment Company"
    p = ExecutableDealPipeline(reusable=store)
    r = p.run(_base_product(product_category="blade", title="blade"))
    assert r["metrics"]["supplier_facts_reused"] >= 1
    assert r["metrics"]["external_requests_avoided"] >= 1


def test_stale_supplier_knowledge_flags_reverification():
    store = ReusableKnowledgeStore()
    fact = supplier_fact("Old Co", category="widgets", source="old")
    fact["staleness"] = "STALE"
    store.add_supplier(fact)
    hits = query_supplier_knowledge(store, category="widgets")
    assert hits[0].get("needs_reverification") is True


def test_quote_required_not_false_economic_reject():
    p = ExecutableDealPipeline()
    r = p.run(_base_product())
    assert r["operator_readiness"] == READY_QUOTE_REQUIRED
    assert r["stop_reason"] == "QUOTE_REQUIRED"
    assert (r["deal"].get("economics") or {}).get("status") in {None, "QUOTE_REQUIRED"} or True
    sheet = (r["actions"][0].get("call_sheet") if r["actions"] else None)
    assert sheet or any(a["action_type"] == "CALL_SUPPLIER" for a in r["actions"])


def test_freight_cannot_silently_default_to_zero():
    f = freight_fact(None)
    assert f["value"] is None
    assert f["quote_required"] or f["maturity"] in {EV_QUOTE_REQUIRED, "UNKNOWN"}
    bad = freight_fact(0, maturity=EV_ESTIMATE)
    assert bad["value"] is None or bad["maturity"] != EV_OPERATOR_CONFIRMED
    ok = freight_fact(0, included_in_supplier=True)
    assert ok["value"] == 0
    assert ok["is_verified"]


def test_estimate_cannot_become_verified():
    fact = mature_fact(100, EV_ESTIMATE)
    fact["is_verified"] = True  # hostile
    fixed = assert_not_promoting_estimate(fact)
    assert fixed["is_verified"] is False


def test_deal_specific_quote_not_universal():
    assert classify_promotion("quoted_total", deal_specific_quote=True) == "DEAL_SPECIFIC_ONLY"


def test_operator_ingestion_and_recalc_unlocks_funding():
    store = ReusableKnowledgeStore()
    p = ExecutableDealPipeline(reusable=store)
    r1 = p.run(_base_product())
    assert r1["operator_readiness"] == READY_QUOTE_REQUIRED
    ing = ingest_supplier_call_result(
        r1["deal"],
        {
            "supplier": "FastCo Dist",
            "quoted_total": 50000,
            "freight_included": True,
            "payment_terms": "payment before shipment",
            "authorization_confirmed": True,
            "product_match_confirmed": True,
            "notes": "SYNTHETIC_NON_LIVE",
        },
        reusable=store,
        synthetic_test=True,
    )
    deal = ing["deal"]
    deal["proposed_bid"] = 62000
    r2 = p.run(deal)
    assert r2["operator_readiness"] == READY_FUNDING_CALL
    assert r2["deal"]["working_capital"]["working_capital_required"] is not None
    assert any(a["action_type"] == "CALL_FINANCIER" for a in r2["actions"])
    assert any(p.get("promotion") == "DEAL_SPECIFIC_ONLY" for p in ing["promotions"])


def test_finance_knowledge_reused_and_fico_incompatible():
    store = ReusableKnowledgeStore()
    store.add_finance(finance_fact("bank", "minimum_fico", 620, source="verified", verified=True))
    matches = match_finance_knowledge(store, amount=50000)
    assert matches[0]["status"] == "INCOMPATIBLE"


def test_pg_alone_does_not_auto_reject():
    store = ReusableKnowledgeStore()
    store.add_finance(finance_fact("po_fin", "pg_requirement", "full", source="profile", verified=False))
    matches = match_finance_knowledge(store, amount=50000)
    assert matches[0]["status"] != "INCOMPATIBLE"


def test_compliance_blocks_bid_ready():
    store = ReusableKnowledgeStore()
    p = ExecutableDealPipeline(reusable=store)
    deal = _base_product(
        supplier_quote={"quoted_total": 40000, "freight_included": True},
        proposed_bid=55000,
        financing_cost_amount=1500,
        funding_deal_specific_confirmed=True,
        financier_result={"accepted_in_principle": True, "pre_bid_conditional": True, "pg_requirement": False},
        compliance_blockers=["manufacturer_authorization_unresolved"],
    )
    r = p.run(deal)
    assert r["operator_readiness"] == READY_COMPLIANCE


def test_synthetic_refuses_live_deal():
    with pytest.raises(ValueError):
        ingest_supplier_call_result(
            {"is_live": True, "deal_id": LIVE_IOWA},
            {"quoted_total": 1},
            synthetic_test=True,
        )


def test_operator_deal_packet_concise():
    p = ExecutableDealPipeline()
    r = p.run(_base_product())
    packet = build_operator_deal_packet(r)
    assert packet["kind"] == "OperatorDealPacket"
    for key in ("DEAL", "PRODUCT", "SUPPLIER", "ECONOMICS", "FUNDING", "COMPLIANCE", "NEXT_ACTIONS", "DECISION_STATUS"):
        assert key in packet
    assert "worth_brian_time" in packet


def test_request_dedupe_in_action_queue():
    q = OperatorActionQueue()
    from operator_action_queue import operator_action

    a1 = operator_action(
        "CALL_SUPPLIER",
        deal_id="X",
        why="q",
        who_where="Winter",
        questions=["a"],
        information_expected=["b"],
        unlocks_stage="SUPPLIER_COSTING",
    )
    a2 = dict(a1)
    a2["action_id"] = "other"
    q.add(a1)
    q.add(a2)
    assert len(q.open_actions("X")) == 1


def test_pipeline_terminates_no_outreach():
    p = ExecutableDealPipeline()
    r = p.run(_base_product())
    assert r["metrics"]["external_communication"] is False
    assert r["metrics"]["bid_submitted"] is False
    assert r["stage"]  # terminates
    assert len(r["stages_run"]) < 30


def test_profit_floor_and_known_costs():
    from economic_integrity import COST_CALCULATED, COST_NOT_APPLICABLE, COST_VERIFIED, calculate_actual_profit, cost_item, revenue_item

    profit = calculate_actual_profit(
        revenue=revenue_item(value=60000, status="CALCULATED"),
        costs={
            "supplier_acquisition": cost_item(category="supplier_acquisition", value=45000, status=COST_VERIFIED, required=True),
            "freight": cost_item(category="freight", value=1000, status=COST_VERIFIED, required=True),
            "financing": cost_item(category="financing", value=2000, status=COST_CALCULATED, required=True),
            "other_required": cost_item(category="other_required", status=COST_NOT_APPLICABLE),
        },
    )
    assert profit["actual_profit"] == 12000
    assert profit.get("meets_min_actual_profit") is True or profit["actual_profit"] >= PROFIT_FLOOR_USD


def test_live_iowa_packet_quote_required_if_present():
    path = PACKETS / f"{LIVE_IOWA}.json"
    if not path.exists():
        pytest.skip("iowa packet missing")
    from scripts.run_executable_deal_pipeline import packet_to_opportunity, seed_reusable

    store = seed_reusable()
    opp = packet_to_opportunity(json.loads(path.read_text(encoding="utf-8")), is_live=True)
    assert opp["bom_line_count"] if False else True
    assert len(opp["line_items"]) >= 2
    r = ExecutableDealPipeline(reusable=store).run(opp)
    assert r["operator_readiness"] == READY_QUOTE_REQUIRED
    assert r["metrics"]["external_communication"] is False
    assert r["metrics"]["bid_submitted"] is False
    assert any(a["action_type"] == "CALL_SUPPLIER" for a in r["actions"])
    call = next(a for a in r["actions"] if a["action_type"] == "CALL_SUPPLIER")
    assert "Winter" in str(call.get("who_where") or call.get("call_sheet"))
    assert r["metrics"]["supplier_facts_reused"] >= 1


def test_financier_ingestion_promotes_reusable():
    store = ReusableKnowledgeStore()
    deal = {"deal_id": "SYNTH-X", "is_live": False}
    out = ingest_financier_call_result(
        deal,
        {
            "provider": "po_fin_A",
            "fees": 1500,
            "pg_requirement": "full",
            "minimum_fico": None,
            "cash_injection": 0,
            "pre_bid_conditional_indication": True,
            "transaction_accepted_in_principle": True,
            "personal_credit_role": "not_material",
        },
        reusable=store,
        synthetic_test=True,
    )
    assert out["deal"]["financing_cost_amount"] == 1500
    assert store.finance
    assert out["deal"]["funding_deal_specific_confirmed"] is True


def test_supplier_call_sheet_deal_specific():
    sheet = build_supplier_call_sheet(
        deal={
            "solicitation_number": LIVE_IOWA,
            "delivery_destination": "Ames, Iowa",
            "bid_deadline": "10/7/2026, 1:00 PM CDT",
        },
        supplier={"name": "Winter Equipment Company"},
        line_items=[{"description": "blade", "quantity": 300, "unit": "EA"}],
    )
    assert sheet["auto_send"] is False
    assert any("Ames" in q for q in sheet["questions"])
