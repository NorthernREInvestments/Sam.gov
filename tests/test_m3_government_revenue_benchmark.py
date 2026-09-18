"""Tests for Government Award History + Revenue Benchmark Engine."""

from __future__ import annotations

from m3_government_revenue_benchmark import (
    BID_TAB_WINNING_PRICE,
    ECONOMICS_SCENARIO_READY,
    HISTORICAL_BENCHMARK_SCENARIO,
    PUBLIC_ECONOMICS_BELOW_TARGET,
    RB_HISTORICAL_SAME_ITEM,
    SAME_SPECIES,
    WHOLESALE_VERIFICATION_REQUIRED,
    analyze_line_revenue,
    apply_va_revenue_update,
    build_bid_price_scenarios,
    build_va_revenue_queues,
    classify_requirement_match,
    deal_room_government_price_history_section,
    inventory_government_identifiers,
    parse_species_unit_prices,
)


SAMPLE_TAB = """
Species List - GRAMINOIDS Allendan Ion Exchange Prairie Moon Shooting Star
Common name Scientific name Cost/lb. PLS Cost/lb. PLS Cost/lb. PLS Cost/lb. PLS
Big bluestem Andropogon gerardii $7.50 $10.40 $8.80 $12.00
Little bluestem Schizachyrium scoparium $15.60 $18.00 $13.00
Switch grass Panicum virgatum $6.00 $13.00 $15.00 $9.20
"""


def test_parse_pdf_multiline_species_blocks():
    pdfish = """
Big bluestem
Andropogon gerardii
$7.50
$10.40
$8.80
$12.00
Little bluestem
Schizachyrium scoparium
$15.60
$18.00
$13.00
"""
    rows = parse_species_unit_prices(pdfish, source_url="https://bidopportunities.iowa.gov/x", agency_hint="Iowa")
    bb = next(r for r in rows if r["Scientific_name"] == "Andropogon gerardii")
    assert bb["Bid_prices"] == [7.5, 10.4, 8.8, 12.0]
    assert bb["Unit_price_low"] == 7.5

    rows = parse_species_unit_prices(
        SAMPLE_TAB,
        source_url="https://bidopportunities.iowa.gov/example",
        agency_hint="Iowa",
    )
    assert len(rows) >= 3
    bb = next(r for r in rows if "Andropogon gerardii" in r["Scientific_name"])
    assert bb["Unit_price_low"] == 7.5
    assert bb["Unit_price_high"] == 12.0
    assert bb["Bid_count"] == 4
    assert bb["Evidence_type"] == BID_TAB_WINNING_PRICE
    assert bb["UOM"] == "LB"


def test_species_match_not_loose_keyword():
    line = {"description": "Big bluestem  (Andropogon gerardii)", "quantity": 324.3, "uom": "LB"}
    inv = {"Opportunity_id": "x", "NSN": "UNKNOWN", "Part_number": "UNKNOWN", "Agency": "Iowa"}
    evidence = {
        "Scientific_name": "Andropogon gerardii",
        "Common_name": "Big bluestem",
        "Source_URL": "https://example.gov",
        "Evidence_type": BID_TAB_WINNING_PRICE,
        "UOM": "LB",
        "Unit_price": 7.5,
    }
    m = classify_requirement_match(line, evidence, inv=inv)
    assert m["Match_type"] == SAME_SPECIES
    assert m["Match_confidence"] in {"HIGH", "MEDIUM"}


def test_bid_scenarios_grounded_and_label_historical():
    scenarios = build_bid_price_scenarios(
        unit_prices=[7.5, 10.4, 12.0],
        quantity=324.3,
        acquisition_unit=13.75,
        acquisition_total=4095.0,
        freight=None,
        financing=None,
        expenses=None,
        target_profit=10000.0,
    )
    assert scenarios
    assert all(s["label"] == HISTORICAL_BENCHMARK_SCENARIO for s in scenarios)
    assert all(s["not_current_verified_revenue"] is True for s in scenarios)
    # public acq above gov bid → negative / below target + wholesale required
    neg = [s for s in scenarios if isinstance(s["Expected_profit"], (int, float)) and s["Expected_profit"] < 0]
    assert neg
    assert PUBLIC_ECONOMICS_BELOW_TARGET in str(neg[0]["status_annotation"])
    assert WHOLESALE_VERIFICATION_REQUIRED in str(neg[0]["status_annotation"])


def test_line_revenue_pairs_seed_acq_with_gov_benchmark():
    row = {
        "canonical_id": "seed-test",
        "title": "Wildflower and Native Grass Seed",
        "agency": "Iowa DOT",
        "public_pricing_evidence": {"normalized_cost": 4095.0},
        "line_items": [
            {"description": "Big bluestem  (Andropogon gerardii)", "quantity": 324.3, "uom": "LB"},
        ],
    }
    inv = inventory_government_identifiers(row)
    evid = parse_species_unit_prices(
        SAMPLE_TAB, source_url="https://bidopportunities.iowa.gov/x", agency_hint="Iowa"
    )
    analysis = analyze_line_revenue(row, row["line_items"][0], inv, evid)
    assert analysis["REVENUE_BASIS"] == RB_HISTORICAL_SAME_ITEM
    assert analysis["HISTORICAL_REVENUE_BENCHMARK_SCENARIO"]["not_current_verified_revenue"] is True
    assert analysis["ECONOMICS_SCENARIO_READY"] is True
    assert analysis["ACQUISITION_EVIDENCE"]["Observed_unit_price"] == 13.75
    assert analysis["BID_PRICE_SCENARIOS"]
    assert analysis["primary_scenario"]["Expected_profit"] != "UNKNOWN"


def test_va_forbidden_and_queues():
    class _S:
        def __init__(self):
            self._rows = {"c1": {"canonical_id": "c1", "title": "t"}}

        def get(self, cid):
            return self._rows.get(cid)

    bad = apply_va_revenue_update(_S(), "c1", action="CONTACT_AGENCY", note="nope")
    assert bad["ok"] is False
    q = build_va_revenue_queues(limit=5)
    assert "queues" in q
    assert ECONOMICS_SCENARIO_READY in q["queues"] or "ECONOMICS_SCENARIO_READY" in q["queues"]


def test_deal_room_section_shape():
    row = {
        "canonical_id": "c1",
        "government_revenue_benchmark": {
            "REVENUE_BASIS": RB_HISTORICAL_SAME_ITEM,
            "ECONOMICS_SCENARIO_READY": True,
            "primary_line": {"Quantity": 10, "BID_PRICE_SCENARIOS": []},
            "GOVERNMENT_PRICE_EVIDENCE": [],
        },
    }
    sec = deal_room_government_price_history_section(row)
    assert sec["kind"] == "DealRoomGovernmentPriceHistory"
    assert "GOVERNMENT_PRICE_HISTORY" in sec
    assert "ECONOMIC_SCENARIOS" in sec
