"""Phase L.6 quote-required economics tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import DEEP_RESEARCH_NO_FIXED_COUNT, STAGE3_NO_ROW_CAP
from phase_l.product_page_resolution import EXACT_VERIFIED
from phase_l.quote_economics import (
    ACCEPTABLE_QUOTE,
    EXCELLENT_QUOTE,
    FAIL_QUOTE,
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    MARGINAL_QUOTE,
    QUOTE_DEPENDENT_POSITIVE,
    UOM_UNRESOLVED,
    acquisition_headroom,
    assess_government_value,
    calculate_max_buy_engine,
    classify_quote_band,
    convert_unknown_lane,
    evaluate_quote_opportunity,
    expected_revenue,
    freight_reserve_for_row,
    quote_bands,
    quote_dependent_positive,
    quote_outcome_simulator,
)


def test_exact_government_value():
    gov = assess_government_value(
        {"title": "Fleet truck"},
        history={"historical_award_unit_price": 48000},
    )
    assert gov["state"] == GOV_VALUE_EXACT
    assert gov["unit_value"] == 48000


def test_strong_government_value():
    gov = assess_government_value({"title": "Server", "budget": 120000, "quantity": 2})
    assert gov["state"] in {GOV_VALUE_STRONG, GOV_VALUE_EXACT}
    assert gov["total_value"] == 120000 or gov["unit_value"]


def test_comparable_or_range_value():
    gov = assess_government_value(
        {"title": "Loader"},
        stage3={"historical_range": {"low": 70000, "high": 85000, "confidence": "APPROXIMATE"}},
    )
    assert gov["state"] in {GOV_VALUE_RANGE, GOV_VALUE_STRONG, GOV_VALUE_COMPARABLE}
    assert gov["unit_low"] == 70000


def test_commercial_category_benchmark_range():
    gov = assess_government_value(
        {"title": "One or More 2027 Ford F-150 Police Responder(s) or Equivalent"},
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder", "commercial_identity_state": "EXACT_VEHICLE_TRIM"},
    )
    assert gov["state"] in {GOV_VALUE_RANGE, GOV_VALUE_COMPARABLE}
    assert gov["unit_value"] and gov["unit_value"] > 40000
    assert "BENCHMARK" in str(gov.get("source") or "")
    assert gov.get("final_award_value") is False


def test_buyer_memory_requires_product_key():
    mem = {"buyers": {"DEPT OF DEFENSE": {"by_product": {"F-150 POLICE RESPONDER": {"samples": [58000], "median_unit_price": 58000}}}}}
    gov_wrong = assess_government_value(
        {"title": "NSN valve assembly", "agency": "DEPT OF DEFENSE"},
        buyer_memory=mem,
        commercial={"model": "VALVE-X"},
    )
    assert gov_wrong["state"] == "GOV_VALUE_UNKNOWN" or gov_wrong.get("source") != "BUYER_PRICE_HISTORY_AVAILABLE"
    gov_ok = assess_government_value(
        {"title": "Ford F-150 Police Responder", "agency": "DEPT OF DEFENSE"},
        buyer_memory=mem,
        commercial={"model": "F-150 Police Responder"},
    )
    # May be RANGE from benchmark OR STRONG from memory
    assert gov_ok["unit_value"] is not None


def test_expected_revenue_range():
    gov = {
        "state": GOV_VALUE_RANGE,
        "unit_value": 77500,
        "unit_low": 70000,
        "unit_high": 85000,
        "quantity": 1,
    }
    rev = expected_revenue(gov)
    assert rev["ExpectedRevenueLow"] == 70000
    assert rev["ExpectedRevenueHigh"] == 85000
    assert rev["ExpectedRevenueMid"] == 77500


def test_max_buy_break_even_and_tiers():
    mb = calculate_max_buy_engine(government_unit=80000, quantity=1, freight_reserve=2000, financing_rate=0.05)
    assert mb is not None
    th = mb["thresholds"]
    assert th["BREAK_EVEN_MAX_BUY"] is not None
    assert th["MAX_BUY_FOR_5K_PROFIT"] is not None
    assert th["MAX_BUY_FOR_10K_PROFIT"] is not None
    assert th["MAX_BUY_FOR_25K_PROFIT"] is not None
    assert th["BREAK_EVEN_MAX_BUY"] > th["MAX_BUY_FOR_10K_PROFIT"]


def test_margin_thresholds():
    mb = calculate_max_buy_engine(government_unit=100000, quantity=1, freight_reserve=1000)
    th = mb["thresholds"]
    assert th["MAX_BUY_FOR_15_PERCENT_MARGIN"] is not None
    assert th["MAX_BUY_FOR_20_PERCENT_MARGIN"] is not None
    assert th["MAX_BUY_FOR_25_PERCENT_MARGIN"] is not None


def test_financing_not_double_counted():
    # Higher financing rate must lower max buy (single application in denom)
    a = calculate_max_buy_engine(government_unit=50000, financing_rate=0.05, freight_reserve=0)
    b = calculate_max_buy_engine(government_unit=50000, financing_rate=0.10, freight_reserve=0)
    assert a["thresholds"]["BREAK_EVEN_MAX_BUY"] > b["thresholds"]["BREAK_EVEN_MAX_BUY"]


def test_freight_reserve():
    fr = freight_reserve_for_row({"title": "2027 Ford F-150 Police Responder"})
    assert fr["amount"] >= 1000
    assert fr["amount"] != 0


def test_quote_target_and_bands():
    mb = calculate_max_buy_engine(government_unit=80000, quantity=1, freight_reserve=2000)
    assert "SUPPLIER_QUOTE_REQUIRED" in (mb.get("label") or "")
    bands = quote_bands(mb)
    assert EXCELLENT_QUOTE in bands
    assert ACCEPTABLE_QUOTE in bands
    assert MARGINAL_QUOTE in bands
    assert FAIL_QUOTE in bands
    target = mb["supplier_quote_target"]
    assert classify_quote_band(target * 0.5, bands) == EXCELLENT_QUOTE
    assert classify_quote_band(target * 1.5, bands) == FAIL_QUOTE


def test_quote_simulator():
    mb = calculate_max_buy_engine(government_unit=80000, quantity=1, freight_reserve=2000)
    sim = quote_outcome_simulator(mb, revenue_mid=80000, freight=2000, quantity=1)
    assert len(sim) == 5
    assert sim[0]["go_no_go"] in {"GO", "NO_GO"}
    assert sim[0]["estimated_net"] > sim[-1]["estimated_net"]


def test_quote_dependent_positive_and_tiers():
    mb = calculate_max_buy_engine(government_unit=80000, quantity=1, freight_reserve=2000)
    gov = {"state": GOV_VALUE_EXACT, "unit_value": 80000}
    q = quote_dependent_positive(max_buy=mb, suppliers=[{"supplier_domain": "x.com"}], gov=gov)
    assert q["state"] == QUOTE_DEPENDENT_POSITIVE
    assert q["tiers"]["quote_dependent_positive"] is True
    assert q["tiers"]["ge_5k"] is True
    assert q["not_verified_profit"] is True


def test_supplier_ranking_in_evaluate():
    ev = evaluate_quote_opportunity(
        {"title": "Bobcat ToolCat UW56 Utility", "agency": "City of Austin", "historical_award_unit_price": 75000},
        commercial={"manufacturer": "Bobcat", "model": "ToolCat UW56"},
        history={"historical_award_unit_price": 75000},
        lane="QUOTE_REQUIRED_COMMERCIAL",
    )
    assert ev["supplier_count"] >= 1
    assert ev["max_buy"] is not None
    assert ev["quote_packet"]["send_authorized"] is False


def test_config_mismatch_downgrade():
    ev = evaluate_quote_opportunity(
        {"title": "Compact track loader base model"},
        history={"historical_award_unit_price": 90000, "description": "turnkey install warranty package"},
    )
    assert ev["government_value"].get("config_mismatch") or ev["government_value"]["state"] in {
        GOV_VALUE_COMPARABLE,
        GOV_VALUE_EXACT,
        GOV_VALUE_STRONG,
    }


def test_uom_unresolved_constant():
    assert UOM_UNRESOLVED == "UOM_UNRESOLVED"


def test_unknown_lane_conversion():
    conv = convert_unknown_lane(
        {"title": "Ford F-150 fleet pickup trucks", "our_bid_access": "YES"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        current_lane="UNKNOWN_ACQUISITION_CHANNEL",
    )
    assert conv["converted"] is True
    assert "QUOTE" in conv["after"] or conv["after"] != "UNKNOWN_ACQUISITION_CHANNEL"


def test_headroom():
    h = acquisition_headroom(max_buy_unit=58000, observed_or_lead=42000)
    assert h["dollars"] == 16000
    assert h["within_target"] is True


def test_no_deep_cap():
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT


def test_final_verified_positive_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_total_basis_max_buy():
    mb = calculate_max_buy_engine(government_total=100000, freight_reserve=3000)
    assert mb["basis"] == "TOTAL"
    assert mb["supplier_quote_target"] is not None
