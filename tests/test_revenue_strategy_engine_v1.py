"""Mandatory financial classification + RSE contribution tests."""

from __future__ import annotations

from decimal import Decimal

from buyer_intelligence.revenue_strategy_engine_v1 import (
    CONDITIONAL_CONTRIBUTION,
    CONTRIBUTION_UNKNOWN,
    LANE_CASH,
    LANE_GROWTH,
    LANE_PROVISIONAL,
    LANE_SCALE,
    VERIFIED_CONTRIBUTION,
    VERIFIED_NEGATIVE_CONTRIBUTION,
    assign_lane_from_contribution,
    calculate_remaining_contribution,
    lowest_supported_purchase_cost,
    normalize_purchase_units,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import FIN_CONFIRMED, FIN_UNKNOWN


def test_award_value_does_not_enter_scale():
    """A $100,000 award alone must not become SCALE."""
    r = calculate_remaining_contribution(
        selling_price=Decimal("100000"),
        acquisition_cost=None,
        freight=None,
        financing_fees=None,
    )
    assert r["CONTRIBUTION_STATE"] == CONTRIBUTION_UNKNOWN
    lane = assign_lane_from_contribution(r)
    assert lane["LANE"] == LANE_PROVISIONAL
    assert lane.get("HYPOTHETICAL_LANE") is None


def test_acquisition_cost_does_not_enter_scale():
    """A $50,000 supplier cost alone must not become SCALE."""
    r = calculate_remaining_contribution(
        selling_price=None,
        acquisition_cost=Decimal("50000"),
        freight=None,
        financing_fees=None,
    )
    assert r["CONTRIBUTION_STATE"] == CONTRIBUTION_UNKNOWN
    lane = assign_lane_from_contribution(r)
    assert lane["LANE"] == LANE_PROVISIONAL
    assert "scale" not in (lane.get("filter_tags") or [])


def test_unknown_selling_price_unknown_contribution():
    r = calculate_remaining_contribution(
        selling_price=None,
        acquisition_cost=Decimal("1000"),
        freight=Decimal("100"),
        financing_fees=Decimal("50"),
        packaging=Decimal("0"),
        compliance=Decimal("0"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == CONTRIBUTION_UNKNOWN
    assert r["REMAINING_CONTRIBUTION"] is None


def test_unknown_freight_not_silent_zero():
    r = calculate_remaining_contribution(
        selling_price=Decimal("10000"),
        acquisition_cost=Decimal("7000"),
        freight=None,
        financing_fees=Decimal("200"),
        packaging=Decimal("0"),
        compliance=Decimal("0"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == CONTRIBUTION_UNKNOWN
    assert "freight" in (r.get("UNKNOWN_COMPONENTS") or [])


def test_unknown_financing_not_silent_zero():
    r = calculate_remaining_contribution(
        selling_price=Decimal("10000"),
        acquisition_cost=Decimal("7000"),
        freight=Decimal("300"),
        financing_fees=None,
        packaging=Decimal("0"),
        compliance=Decimal("0"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == CONTRIBUTION_UNKNOWN
    assert "financing_fees" in (r.get("UNKNOWN_COMPONENTS") or [])


def test_conditional_separate_from_verified():
    r = calculate_remaining_contribution(
        selling_price=None,
        acquisition_cost=Decimal("48178.80"),
        freight=None,
        financing_fees=None,
        allow_conditional=True,
        conditional_assumptions={
            "selling_price": "60000",
            "acquisition_cost": "48178.80",
            "freight": "2000",
            "financing_fees": "1000",
        },
    )
    assert r["CONTRIBUTION_STATE"] == CONDITIONAL_CONTRIBUTION
    lane = assign_lane_from_contribution(r)
    assert lane["LANE"] == LANE_PROVISIONAL
    assert lane["LANE_VERIFICATION"] == "CONDITIONAL"
    assert lane["HYPOTHETICAL_LANE"] in {LANE_CASH, LANE_GROWTH, LANE_SCALE, "BELOW_THRESHOLD"}
    # Must not put verified lane tags
    assert "cash_flow" not in (lane.get("filter_tags") or [])
    assert "growth" not in (lane.get("filter_tags") or [])
    assert "scale" not in (lane.get("filter_tags") or [])


def test_negative_margin_classified():
    r = calculate_remaining_contribution(
        selling_price=Decimal("5000"),
        acquisition_cost=Decimal("7000"),
        freight=Decimal("200"),
        financing_fees=Decimal("100"),
        packaging=Decimal("0"),
        compliance=Decimal("0"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == VERIFIED_NEGATIVE_CONTRIBUTION
    lane = assign_lane_from_contribution(r, financing_status=FIN_CONFIRMED, full_sourcing_ready=True)
    assert Decimal(r["REMAINING_CONTRIBUTION"]) < 0


def test_profit_threshold_uses_contribution_not_markup():
    """$20k award with $19.6k costs → ~$400 contribution → not GROWTH/SCALE."""
    r = calculate_remaining_contribution(
        selling_price=Decimal("20000"),
        acquisition_cost=Decimal("19000"),
        freight=Decimal("400"),
        financing_fees=Decimal("200"),
        packaging=Decimal("0"),
        compliance=Decimal("0"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == VERIFIED_CONTRIBUTION
    assert Decimal(r["REMAINING_CONTRIBUTION"]) == Decimal("400.00")
    lane = assign_lane_from_contribution(r, financing_status=FIN_CONFIRMED, full_sourcing_ready=True)
    assert lane["LANE"] != LANE_SCALE
    assert lane["LANE"] != LANE_GROWTH
    # 400 < 500 → BELOW_THRESHOLD
    assert lane["LANE"] == "BELOW_THRESHOLD"


def test_verified_cash_flow_lane():
    r = calculate_remaining_contribution(
        selling_price=Decimal("10000"),
        acquisition_cost=Decimal("7000"),
        freight=Decimal("300"),
        financing_fees=Decimal("200"),
        packaging=Decimal("50"),
        compliance=Decimal("50"),
        other_direct_costs=Decimal("0"),
    )
    assert r["CONTRIBUTION_STATE"] == VERIFIED_CONTRIBUTION
    assert Decimal(r["REMAINING_CONTRIBUTION"]) == Decimal("2400.00")
    lane = assign_lane_from_contribution(r, financing_status=FIN_CONFIRMED, full_sourcing_ready=True)
    assert lane["LANE"] == LANE_CASH
    assert "cash_flow" in lane["filter_tags"]


def test_volume_tier_selection():
    cost = lowest_supported_purchase_cost(
        required_purchase_qty=Decimal("1160"),
        price_tiers=[
            {"min_qty": 1, "max_qty": 1, "unit_price": "34.99", "source": "list"},
            {"min_qty": 2, "unit_price": "32.99", "source": "tier"},
        ],
    )
    assert cost["SUPPORTED"] is True
    assert cost["UNIT_PRICE"] == "32.99"
    assert cost["PRODUCT_COST"] == "38268.40"


def test_pack_normalization_tm1616s():
    n = normalize_purchase_units(
        solicitation_qty=Decimal("15360"),
        solicitation_uom="Roll (s)",
        pack_qty=Decimal("96"),
        pack_uom="CASE",
    )
    assert n["PURCHASE_QTY"] == "160"
    assert n["PURCHASE_UOM"] == "CASE"
