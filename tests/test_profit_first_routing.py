"""Profit-first economics routing — anatomy tests, not category tests."""

from __future__ import annotations

import pytest

from profit_first.config import (
    LIKELY_PROFITABLE,
    POSSIBLE_PROFIT,
    PROFITABLE_AT_PUBLIC_RETAIL,
    PROVEN_PROFITABLE,
    UNPROFITABLE,
    UNPROVEN,
)
from profit_first.router import evaluate_opportunity_profit, research_next_priority
from profit_first.economics import compute_expected_profit


@pytest.fixture()
def iso_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("M3_MIN_EXPECTED_PROFIT_USD", "5000")
    monkeypatch.setenv("M3_MIN_POST_FINANCING_PROFIT_USD", "2500")
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    yield tmp_path
    set_data_root(None)


def test_pass_case_a_single_retail_profit_no_wholesale(iso_root):
    """Gov $30k, retail $18k, freight+fin $4k → $8k — profitable without wholesale."""
    ev = evaluate_opportunity_profit(
        opportunity_id="PASS-A",
        title="Exact commercial item NSN 1234",
        expected_revenue=30000,
        product_cost=18000,
        freight=2000,
        financing=2000,
        price_basis="PUBLIC_RETAIL",
        completeness_pct=100,
        evidence_grade="A",
        execution_pass=True,
        ranking_signals={"exact_identity": True, "public_retail_available": True},
    )
    econ = ev["economics"]
    assert econ["post_financing_profit"] == 8000
    assert econ["profit_status"] in {PROVEN_PROFITABLE, LIKELY_PROFITABLE}
    assert PROFITABLE_AT_PUBLIC_RETAIL in econ["proof_signals"]
    assert ev["route"] == "OWNER_CANDIDATE"
    card = ev["owner_card"]
    assert card["expected_profit"] == 8000
    assert "retail" in card["price_basis"].lower()


def test_pass_case_b_multiline_basket(iso_root):
    from line_item_economics.engine import analyze_line_item_economics

    rows = [
        {"clin": str(i), "description": f"Supply item {i}", "quantity": 100, "uom": "EA", "part_number": f"P{i}"}
        for i in range(1, 41)
    ]
    # Aggregate: retail 55k, hist 80k across 40 lines → unit hist 20, retail 13.75 for qty 100
    retail = {str(i): {"unit_price": 13.75, "uom": "EA"} for i in range(1, 41)}
    hist = {str(i): {"awarded_unit_price": 20.0, "match_quality": "EXACT"} for i in range(1, 41)}
    lie = analyze_line_item_economics(
        opportunity_id="PASS-B",
        title="Common product basket",
        schedule_rows=rows,
        retail_by_line=retail,
        historical_by_line=hist,
        freight={"public_shipping_quote": 5000},
        financing_cost=4000,
        persist=False,
    )
    assert abs(lie["rollup"]["TOTAL_KNOWN_RETAIL_COST"] - 55000) < 1
    assert abs(lie["rollup"]["TOTAL_KNOWN_HISTORICAL_VALUE"] - 80000) < 1
    ev = evaluate_opportunity_profit(
        opportunity_id="PASS-B",
        title="Common product basket",
        line_item_analysis=lie,
        freight=5000,
        financing=4000,
        execution_pass=True,
        ranking_signals={"multiline_priceable": True, "public_retail_available": True},
    )
    assert ev["economics"]["post_financing_profit"] == 16000
    assert ev["economics"]["profit_status"] in {PROVEN_PROFITABLE, LIKELY_PROFITABLE}
    assert ev["route"] == "OWNER_CANDIDATE"


def test_pass_case_c_specialty_oem_not_rejected(iso_root):
    """Specialty part with $13k profit must not be rejected for being OEM."""
    ev = evaluate_opportunity_profit(
        opportunity_id="PASS-C",
        title="OEM specialty compressor part exact MPN",
        expected_revenue=50000,
        product_cost=31000,
        freight=3000,
        financing=3000,
        price_basis="QUOTED",
        completeness_pct=100,
        evidence_grade="A",
        execution_pass=True,
        ranking_signals={"exact_identity": True},  # no commercially_common boost required
    )
    assert ev["economics"]["post_financing_profit"] == 13000
    assert ev["economics"]["profit_status"] in {PROVEN_PROFITABLE, LIKELY_PROFITABLE}
    assert ev["route"] == "OWNER_CANDIDATE"
    # Ranking must not crash specialty
    assert ev["ranking"]["profit_probability_score"] >= 60


def test_fail_case_a_attractive_category_negative(iso_root):
    """Tools basket looks attractive but -$2k must fail."""
    ev = evaluate_opportunity_profit(
        opportunity_id="FAIL-A",
        title="Common tools basket MRO supplies",
        expected_revenue=25000,
        product_cost=23000,
        freight=2500,
        financing=1500,
        price_basis="PUBLIC_RETAIL",
        completeness_pct=100,
        evidence_grade="A",
        execution_pass=True,
        ranking_signals={"commercially_common": True, "multiline_priceable": True},
    )
    assert ev["economics"]["post_financing_profit"] == -2000
    assert ev["economics"]["profit_status"] == UNPROFITABLE
    assert ev["route"] == "HIDE_OR_REJECT"


def test_fail_case_b_freight_install_kills_spread(iso_root):
    ev = evaluate_opportunity_profit(
        opportunity_id="FAIL-B",
        title="Office furniture systems",
        expected_revenue=100000,
        product_cost=82000,  # gross spread 18000
        freight=12000,
        financing=0,
        other_costs=10000,  # install
        price_basis="PUBLIC_RETAIL",
        completeness_pct=100,
        evidence_grade="B",
        execution_pass=True,
    )
    # 100k - 82k - 12k - 0 - 10k = -4k
    assert ev["economics"]["post_financing_profit"] == -4000
    assert ev["economics"]["profit_status"] == UNPROFITABLE


def test_unknown_case_routes_to_gov_value(iso_root):
    ev = evaluate_opportunity_profit(
        opportunity_id="UNK-1",
        title="Exact model commercial pump",
        product_cost=55000,
        freight=2000,
        financing=1000,
        price_basis="PUBLIC_RETAIL",
        ranking_signals={"exact_identity": True},
    )
    assert ev["economics"]["profit_status"] == UNPROVEN
    assert ev["research"]["priority"] == "GOVERNMENT_VALUE"
    assert ev["route"] == "CONTINUE_RESEARCH"


def test_research_priority_acquisition_when_gov_known(iso_root):
    econ = compute_expected_profit(expected_revenue=100000, product_cost=None)
    r = research_next_priority(econ, identity_known=True)
    assert r["priority"] == "ACQUISITION_COST"


def test_research_priority_freight_when_spread_known(iso_root):
    econ = compute_expected_profit(
        expected_revenue=100000,
        product_cost=80000,
        freight=None,
        financing=None,
    )
    r = research_next_priority(econ, identity_known=True)
    assert r["priority"] == "FREIGHT"


def test_plumbing_case_maps_through_profit_first(iso_root):
    """Canonical plumbing opp lesson: economics, not 'find plumbing'."""
    from line_item_economics.engine import analyze_line_item_economics

    rows = [
        {"clin": str(i), "description": f"Plumbing part {i}", "quantity": 50, "uom": "EA", "part_number": f"PL-{i}"}
        for i in range(1, 41)
    ]
    # Approximate the known case economics via uniform lines
    # retail ~14183 / (40*50) ≈ 7.09; hist ~21262/(40*50)≈10.63
    retail = {str(i): {"unit_price": 7.0914, "uom": "EA"} for i in range(1, 38)}
    hist = {str(i): {"awarded_unit_price": 10.631, "match_quality": "EXACT"} for i in range(1, 35)}
    lie = analyze_line_item_economics(
        opportunity_id="5e2a1594e10401f9",
        title="Various Plumbing Hardware Parts",
        schedule_rows=rows,
        retail_by_line=retail,
        historical_by_line=hist,
        freight={"public_shipping_quote": 2900},
        financing_cost=1400,
        persist=False,
    )
    ev = evaluate_opportunity_profit(
        opportunity_id="5e2a1594e10401f9",
        title="Various Plumbing Hardware Parts",
        line_item_analysis=lie,
        execution_pass=True,
        ranking_signals={"multiline_priceable": True, "public_retail_available": True},
    )
    assert ev["economics"]["gross_spread"] and ev["economics"]["gross_spread"] > 4000
    assert ev["economics"]["profit_status"] in {LIKELY_PROFITABLE, PROVEN_PROFITABLE, POSSIBLE_PROFIT}
    # Lesson preserved: public retail basis
    assert ev["economics"]["price_basis"] == "PUBLIC_RETAIL"
    assert "plumbing" not in (ev.get("route") or "").lower()
