"""Data integrity / no-fabrication regression tests. Zero live APIs."""

from __future__ import annotations

from types import SimpleNamespace

import ai_funnel
from ai_stage1 import normalize_stage1_result
from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_CALCULATED,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    assessment_fact,
    calculated_fact,
    can_hard_reject_on_fact,
    display_fact,
    is_assessment,
    is_unknown,
    is_verified,
    parse_money,
    parse_quantity,
    parse_square_footage,
    serialize_fact_for_api,
    unknown_fact,
    verified_fact,
)
from display_format import format_bool_or_unknown, format_money_or_unknown


def test_missing_contract_value_is_unknown_not_zero():
    fact = unknown_fact(source_field="estimated_value")
    assert fact["value"] is None
    assert fact["status"] == STATUS_UNKNOWN
    assert format_money_or_unknown(fact) == "Unknown"
    assert (
        ai_funnel.resolve_known_contract_value(
            SimpleNamespace(estimated_value=None, sam_raw={}, analysis={})
        )
        is None
    )


def test_missing_quantity_is_unknown_not_one():
    fact = unknown_fact(source_field="quantity")
    assert fact["value"] is None
    assert parse_quantity(None) is None
    assert parse_quantity("") is None
    result = normalize_stage1_result(
        {
            "category": "UNKNOWN",
            "buying": "x",
            "quantity": None,
            "reseller_fit": "NONE",
            "advance": True,
            "reason_code": "UNSTATED",
        }
    )
    assert result["quantity"] is None
    assert result["quantity_fact"]["status"] == STATUS_UNKNOWN


def test_missing_supplier_price_freight_bid_financing_unknown():
    for field in ("supplier_price", "freight", "bid_count", "financing_rate"):
        fact = unknown_fact(source_field=field)
        assert fact["value"] is None
        assert fact["status"] == STATUS_UNKNOWN
        assert display_fact(fact) == "Unknown"


def test_missing_bond_and_license_unknown_not_false():
    result = normalize_stage1_result(
        {
            "category": "LABOR_HEAVY",
            "buying": "grounds",
            "quantity": None,
            "bond_likely": None,
            "license_likely": None,
            "reseller_fit": "NONE",
            "advance": True,
            "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
        }
    )
    assert result["bond_likely"] is None
    assert result["license_likely"] is None
    assert result["bond_fact"]["status"] == STATUS_UNKNOWN
    assert result["license_fact"]["status"] == STATUS_UNKNOWN
    assert format_bool_or_unknown(result["bond_fact"]) == "Unknown"
    assert format_bool_or_unknown(False) == "No"


def test_building_number_not_quantity_or_money():
    assert parse_quantity("Building 977") is None
    assert parse_money("Building 977", allow_loose=True) is None
    assert parse_money("Building 977", allow_loose=False) is None
    info = ai_funnel.resolve_known_contract_value(
        SimpleNamespace(
            estimated_value=None,
            title="Services at Building 977",
            description="Work at Building 977 Kapolei",
            sam_raw={},
            analysis={},
        )
    )
    assert info is None


def test_naics_and_zip_not_money_in_free_text():
    assert parse_money("NAICS 561730", allow_loose=True) is None
    assert parse_money("ZIP 96707", allow_loose=True) is None
    assert parse_money("Kapolei HI 96707", allow_loose=True) is None


def test_verified_monetary_field_retains_provenance():
    opp = SimpleNamespace(
        estimated_value="27081.00",
        sam_raw={"award": {"amount": 27081.00}},
        analysis={},
    )
    info = ai_funnel.resolve_known_contract_value(opp)
    assert info is not None
    assert info["amount"] == 27081.0
    assert info["status"] == STATUS_VERIFIED
    assert info["confidence"] == "HIGH"
    assert "award.amount" in str(info.get("source_field"))
    assert can_hard_reject_on_fact(info) is True


def test_calculated_value_retains_input_provenance():
    units = verified_fact(50, source_type="SOLICITATION", source_field="quantity")
    price = verified_fact(412.50, source_type="SUPPLIER_QUOTE", source_field="unit_cost")
    total = calculated_fact(
        50 * 412.50,
        calculation="quantity * unit_cost",
        inputs=[units, price],
        source_field="supplier_cost",
    )
    assert total["status"] == STATUS_CALCULATED
    assert total["value"] == 20625.0
    assert len(total["inputs"]) == 2
    assert all(is_verified(i) for i in total["inputs"])


def test_ai_assessment_cannot_become_verified_or_hard_reject():
    assessment = assessment_fact(27081.0, source_type="AI", source_field="analysis.estimated_value")
    assert is_assessment(assessment)
    assert not is_verified(assessment)
    assert can_hard_reject_on_fact(assessment) is False

    opp = SimpleNamespace(
        estimated_value=None,
        sam_raw={},
        analysis={"estimated_value": "$6,000"},
        notice_id="n1",
        title="Small buy",
        description="supplies",
        due_date=None,
        status="new",
        set_aside="Total Small Business",
        naics_code="561730",
        agency="X",
        location="HI",
        link=None,
    )
    info = ai_funnel.resolve_known_contract_value(opp)
    assert info is not None
    assert info["status"] == STATUS_ASSESSMENT
    assert can_hard_reject_on_fact(info) is False
    r = ai_funnel.stage0_evaluate(opp)
    assert "value_below_min_profit" not in (r.get("reject_reasons") or [])


def test_unknown_cannot_trigger_factual_stage0_rejection():
    opp = SimpleNamespace(
        notice_id="x",
        title="Groundskeeping",
        description="mowing",
        due_date=None,
        status="new",
        set_aside="Total Small Business",
        naics_code="561730",
        agency="NUWC",
        location="Kapolei, HI",
        analysis={},
        sam_raw={},
        estimated_value=None,
        link=None,
    )
    r = ai_funnel.stage0_evaluate(opp)
    assert "value_unknown" in r["flags"]
    assert "value_below_min_profit" not in r.get("reject_reasons", [])
    assert r["decision"] in {"ADVANCE", "REVIEW"}


def test_failed_parser_returns_unknown_not_plausible_fallback():
    from prior_contract_extract import _parse_money_amount
    from pws_fields import _parse_frequency, _parse_int
    from watchlist_fingerprint import parse_dollar_amount

    assert _parse_money_amount("N/A") is None
    assert _parse_money_amount("see Building 977") is None
    assert parse_dollar_amount("Building 977 near ZIP 96707") is None
    assert _parse_int("Building 977") is None
    assert _parse_frequency("see schedule section 3") is None
    assert parse_square_footage("Building 977") is None


def test_ui_api_serialization_preserves_unknown_vs_zero_false():
    unk = unknown_fact(source_field="bid_count")
    zero = verified_fact(0, source_type="SAM", source_field="number_of_offers")
    false_bond = verified_fact(False, source_type="SOLICITATION", source_field="bond_required")

    ser_unk = serialize_fact_for_api(unk)
    assert ser_unk["value"] is None
    assert ser_unk["status"] == STATUS_UNKNOWN

    assert format_money_or_unknown(unk) == "Unknown"
    assert format_bool_or_unknown(false_bond) == "No"
    assert format_bool_or_unknown(unk) == "Unknown"
    assert serialize_fact_for_api(zero)["value"] == 0
    assert serialize_fact_for_api(zero)["status"] == STATUS_VERIFIED


def test_intake_does_not_promote_ai_value_to_orm():
    from data_integrity import assessment_fact as af

    analysis = {"estimated_value": "$12,000"}
    row = SimpleNamespace(estimated_value=None, analysis=analysis)
    if analysis.get("estimated_value") and not row.estimated_value:
        analysis["estimated_value_fact"] = af(
            analysis["estimated_value"],
            source_type="AI",
            source_field="analysis.estimated_value",
        )
    assert row.estimated_value is None
    assert analysis["estimated_value_fact"]["status"] == STATUS_ASSESSMENT


def test_annual_award_unknown_period_not_fabricated():
    from usaspending_client import estimate_annual_award_amount

    assert estimate_annual_award_amount({"award_amount": 100000}) is None
    assert (
        estimate_annual_award_amount(
            {"award_amount": 100000, "start_date": "2020-01-01", "end_date": "2022-01-01"}
        )
        == 50000.0
    )


def test_comparable_scope_omits_unit_rate_without_annual_basis():
    from comparable_scope import annotate_award_unit_rates

    row = annotate_award_unit_rates(
        {"award_amount": 100000, "description": "janitorial 10000 sq ft weekly cleaning"}
    )
    assert row["award_amount_basis"] == "total"
    assert row["award_annual_amount"] is None
    assert row["price_per_sqft_per_visit"] is None


def test_is_unknown_helpers():
    assert is_unknown(None)
    assert is_unknown(unknown_fact())
    assert not is_unknown(verified_fact(1, source_type="SAM", source_field="x"))
