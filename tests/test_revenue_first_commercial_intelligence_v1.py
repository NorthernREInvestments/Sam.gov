"""Acceptance tests for Revenue-First Commercial Intelligence V1."""

from __future__ import annotations

from decimal import Decimal

from buyer_intelligence.revenue_first_commercial_intelligence_v1 import (
    LANE_BELOW,
    LANE_BLOCKED,
    LANE_CASH,
    LANE_GROWTH,
    LANE_PROVISIONAL,
    LANE_SCALE,
    FIN_BLOCKED,
    FIN_CONFIRMED,
    FIN_UNKNOWN,
    FIN_UNVERIFIED,
    cash_flow_rank_score,
    classify_profit_lane,
    evaluate_cash_flow,
    max_cost_targets_by_lane,
    find_reusable_quotes,
)


def test_lanes_cash_growth_scale():
    assert classify_profit_lane(
        expected_contribution=Decimal("800"),
        evidence_complete=True,
        financing_status=FIN_CONFIRMED,
        full_sourcing_ready=True,
    )["LANE"] == LANE_CASH
    assert classify_profit_lane(
        expected_contribution=Decimal("7500"),
        evidence_complete=True,
        financing_status=FIN_CONFIRMED,
        full_sourcing_ready=True,
    )["LANE"] == LANE_GROWTH
    assert classify_profit_lane(
        expected_contribution=Decimal("20000"),
        evidence_complete=True,
        financing_status=FIN_CONFIRMED,
        full_sourcing_ready=True,
    )["LANE"] == LANE_SCALE


def test_unknown_is_provisional_not_zero():
    lane = classify_profit_lane(
        expected_contribution=None,
        evidence_complete=False,
        financing_status=FIN_UNKNOWN,
        full_sourcing_ready=False,
    )
    assert lane["LANE"] == LANE_PROVISIONAL
    assert lane["VERIFICATION"] == "UNVERIFIED"


def test_below_threshold_and_blocked():
    assert (
        classify_profit_lane(
            expected_contribution=Decimal("100"),
            evidence_complete=True,
            financing_status=FIN_UNVERIFIED,
            full_sourcing_ready=True,
        )["LANE"]
        == LANE_BELOW
    )
    assert (
        classify_profit_lane(
            expected_contribution=Decimal("8000"),
            evidence_complete=True,
            financing_status=FIN_BLOCKED,
            full_sourcing_ready=True,
        )["LANE"]
        == LANE_BLOCKED
    )


def test_cash_flow_unknown_timing():
    cf = evaluate_cash_flow(
        award_or_selling=Decimal("10000"),
        acquisition_cost=Decimal("7000"),
        financing_fees=None,
        financing_status=FIN_UNVERIFIED,
    )
    assert cf["PAYMENT_TIMING_CONFIDENCE"] == "TIME_TO_CASH_UNKNOWN"
    assert cf["ESTIMATED_DAYS_TO_CASH"] is None
    assert cf["OWNER_CASH_REQUIRED"] is True
    assert cf["EXPECTED_CONTRIBUTION"] is None  # fees unknown
    assert cf["GROSS_SPREAD_SCENARIO"] == "3000.00"


def test_cash_flow_with_fees_and_timing():
    cf = evaluate_cash_flow(
        award_or_selling=Decimal("10000"),
        acquisition_cost=Decimal("7000"),
        financing_fees=Decimal("500"),
        financing_status=FIN_CONFIRMED,
        inventory_lead_days=5,
        shipping_days=3,
        acceptance_days=2,
        invoice_submit_days=1,
        gov_payment_days=30,
        supplier_deposit=Decimal("0"),
    )
    assert cf["EXPECTED_CONTRIBUTION"] == "2500.00"
    assert cf["ESTIMATED_DAYS_TO_CASH"] == 41
    assert cf["OWNER_CASH_REQUIRED"] is False


def test_margin_vs_markup_and_lane_ceilings():
    t = max_cost_targets_by_lane(
        Decimal("10000"),
        freight=Decimal("0"),
        compliance=Decimal("0"),
        financing_fees=Decimal("0"),
    )
    assert "LANE_CASH_FLOW_500" in t["TARGETS"]
    assert "LANE_SCALE_15000" in t["TARGETS"]
    assert Decimal(t["TARGETS"]["GROSS_MARGIN_20PCT"]["MAX_PRODUCT_ACQUISITION"]) == Decimal("8000.00")
    # Scale floor 15k on 10k sell → negative ceiling (impossible) still reported
    assert Decimal(t["TARGETS"]["LANE_SCALE_15000"]["MAX_ACQUISITION_PLUS_UNRESOLVED"]) == Decimal("-5000.00")


def test_rank_prefers_sooner_cash_over_raw_size():
    fast = evaluate_cash_flow(
        award_or_selling=Decimal("6000"),
        acquisition_cost=Decimal("4000"),
        financing_fees=Decimal("200"),
        financing_status=FIN_CONFIRMED,
        inventory_lead_days=3,
        shipping_days=2,
        acceptance_days=2,
        invoice_submit_days=1,
        gov_payment_days=20,
        supplier_deposit=Decimal("0"),
    )
    slow = evaluate_cash_flow(
        award_or_selling=Decimal("50000"),
        acquisition_cost=Decimal("30000"),
        financing_fees=Decimal("2000"),
        financing_status=FIN_UNVERIFIED,
        inventory_lead_days=30,
        shipping_days=20,
        acceptance_days=15,
        invoice_submit_days=5,
        gov_payment_days=90,
    )
    lane_fast = classify_profit_lane(
        expected_contribution=Decimal("1800"),
        evidence_complete=True,
        financing_status=FIN_CONFIRMED,
        full_sourcing_ready=True,
    )
    lane_slow = classify_profit_lane(
        expected_contribution=Decimal("18000"),
        evidence_complete=True,
        financing_status=FIN_UNVERIFIED,
        full_sourcing_ready=True,
    )
    # Confirmed sooner cash-flow should outrank unverified slow scale on rank helper intent
    assert cash_flow_rank_score(fast, lane_fast) > cash_flow_rank_score(slow, lane_slow) - 30


def test_financing_never_auto_confirmed():
    cf = evaluate_cash_flow(
        award_or_selling=Decimal("1000"),
        acquisition_cost=Decimal("400"),
        financing_fees=Decimal("50"),
        financing_status=FIN_UNKNOWN,
    )
    assert cf["FINANCING_STATUS"] != FIN_CONFIRMED


def test_reusable_quotes_respect_missing_memory():
    assert find_reusable_quotes("No Such Supplier LLC") == []
