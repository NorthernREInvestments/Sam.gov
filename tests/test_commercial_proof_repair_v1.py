"""Tests for Commercial Proof Repair V1."""

from __future__ import annotations

from decimal import Decimal

from buyer_intelligence.commercial_proof_repair_v1 import (
    CATEGORY_ONLY,
    COMPARABLE_CATEGORY_ONLY,
    EXACT_PRODUCT_VERIFIED,
    PARTIAL_PRODUCT_IDENTITY,
    SPECIFICATION_COMPLETE_GENERIC,
    classify_historical_comparability,
    classify_product_identity,
    compute_commercial_economics,
    reclassify_prior_sprint,
)


def test_generic_fsc_titles_are_not_exact():
    for title in ("VALVE,CHECK", "GASKET MATERIAL", "CABLE ASSEMBLY,SPECIAL", "VALVE, FLOW CONTROL"):
        cls = classify_product_identity(title)
        assert cls["IDENTITY_CLASS"] in {CATEGORY_ONLY, PARTIAL_PRODUCT_IDENTITY}
        assert cls["IS_EXACT_VERIFIED"] is False


def test_model_mpn_is_exact():
    cls = classify_product_identity("Penn Valley Pump Model 6DDSX76CNU-MK2 for Beaver Brook")
    assert cls["IDENTITY_CLASS"] == EXACT_PRODUCT_VERIFIED
    assert cls["IS_EXACT_VERIFIED"] is True


def test_paper_towels_are_complete_generic_not_exact():
    cls = classify_product_identity("Paper Towels and Toilet Paper (Bid-Discount)")
    assert cls["IDENTITY_CLASS"] == SPECIFICATION_COMPLETE_GENERIC
    assert cls["IS_EXACT_VERIFIED"] is False


def test_category_only_battery_equipment():
    cls = classify_product_identity("ONE-TIME PURCHASE OF BATTERY-OPERATED EQUIPMENT")
    assert cls["IDENTITY_CLASS"] == CATEGORY_ONLY


def test_check_valve_hist_downgraded_from_generic_open():
    hist = {
        "STATUS": "EXACT_HISTORICAL_UNIT_PRICE",
        "title": 'CHECK VALVE, 8", MODEL 92LW, WITH 18" LAY LENGTH',
        "unit_price": "6393.21",
        "source_url": "https://example.com",
    }
    fixed = classify_historical_comparability("VALVE,CHECK", hist)
    assert fixed["STATUS"] == COMPARABLE_CATEGORY_ONLY
    assert fixed.get("unit_price_for_margin") is None


def test_economics_unknown_freight_not_zero():
    econ = compute_commercial_economics(
        identity_class=SPECIFICATION_COMPLETE_GENERIC,
        qty=Decimal("10"),
        unit_acquisition=Decimal("5"),
        selling_unit=Decimal("20"),
        selling_label="SPECIFICATION_MATCHED_HISTORICAL_PRICE",
        freight=None,
        financing_fees=None,
    )
    assert "FREIGHT" in econ["MISSING"]
    assert econ["REMAINING_CONTRIBUTION_VERIFIED"] is None
    assert econ["GROSS_PROFIT_SCENARIO"] == "150.00"
    assert econ["POSITIVE_PRELIMINARY_MARGIN"] is True
    assert econ["MIN_BID_TARGETS"]["MIN_BID_FOR_500"]["LABEL"] == "TARGET_NOT_VERIFIED_ACHIEVABLE"


def test_prior_sprint_reclass_counts_false_exact():
    prior = [
        {"OPPORTUNITY_ID": "1", "Product": "VALVE,CHECK", "Exact_Product_Identity": True},
        {"OPPORTUNITY_ID": "2", "Product": "Penn Valley Pump Model 6DDSX76CNU-MK2", "Exact_Product_Identity": True},
        {"OPPORTUNITY_ID": "3", "Product": "Paper Towels and Toilet Paper", "Exact_Product_Identity": True},
    ]
    out = reclassify_prior_sprint(prior)
    assert out["PREVIOUS_CLAIMED_EXACT"] == 3
    assert out["ACTUALLY_EXACT_VERIFIED"] == 1
    assert out["FALSE_EXACT_COUNT"] == 2
