"""Regression: authoritative PROVEN / VERIFY / REJECT commercial decision."""

from __future__ import annotations

from decimal import Decimal

import pytest

from verified_commercial_decision_v1 import (
    PROVEN,
    REJECT,
    VERIFY,
    evaluate_verified_commercial_decision,
    load_commercial_targets,
)
from profit_first.economics import compute_expected_profit, from_line_item_rollup


@pytest.fixture()
def targets_5k_15(monkeypatch):
    monkeypatch.setenv("M3_MIN_EXPECTED_PROFIT_USD", "5000")
    monkeypatch.setenv("M3_MIN_MARGIN_PCT", "15")
    return load_commercial_targets()


def _base_proven_kwargs(**overrides):
    kw = dict(
        proposed_bid_revenue=30000,
        product_acquisition=18000,
        product_acquisition_status="VERIFIED",
        inbound_freight=None,
        inbound_freight_status="NOT_APPLICABLE",
        inbound_freight_reason="included_in_outbound",
        outbound_freight=2000,
        outbound_freight_status="VERIFIED",
        packaging_status="NOT_APPLICABLE",
        packaging_reason="included",
        insurance_status="NOT_APPLICABLE",
        insurance_reason="not_required",
        financing_cost=2000,
        financing_status="VERIFIED",
        taxes_status="NOT_APPLICABLE",
        taxes_reason="na",
        payment_processing_status="NOT_APPLICABLE",
        payment_processing_reason="na",
        compliance_status="NOT_APPLICABLE",
        compliance_reason="na",
        warranty_status="NOT_APPLICABLE",
        warranty_reason="na",
        overhead_status="NOT_APPLICABLE",
        overhead_reason="na",
        other_status="NOT_APPLICABLE",
        other_reason="na",
        financing_transaction_verified=True,
        personal_cash_required_usd=0,
        product_only=True,
        prime_eligible=True,
        deadline_open=True,
        evidence_current=True,
        sourcing_complete=True,
        line_count_required=3,
        line_count_sourced=3,
    )
    kw.update(overrides)
    return kw


def test_margin_14_99_fails(targets_5k_15):
    # revenue 10000, costs 8501 → profit 1499 → margin 14.99%
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(
            proposed_bid_revenue=10000,
            product_acquisition=6501,
            outbound_freight=1000,
            financing_cost=1000,
        ),
        targets=targets_5k_15,
    )
    assert d["net_margin_pct"] == pytest.approx(14.99, abs=0.01)
    assert d["readiness"] == REJECT
    assert "MARGIN_BELOW_FLOOR" in d["missing_requirements"]


def test_margin_15_00_passes_margin_test(targets_5k_15):
    # revenue 100000, costs 85000 → profit 15000 = 15%
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(
            proposed_bid_revenue=100000,
            product_acquisition=70000,
            outbound_freight=8000,
            financing_cost=7000,
        ),
        targets=targets_5k_15,
    )
    assert d["net_margin"] == Decimal("0.1500") or float(d["net_margin"]) == pytest.approx(0.15, abs=1e-4)
    assert d["margin_ok"] is True
    assert d["readiness"] == PROVEN


def test_margin_20_passes(targets_5k_15):
    d = evaluate_verified_commercial_decision(**_base_proven_kwargs(), targets=targets_5k_15)
    assert float(d["net_margin"]) >= 0.20
    assert d["readiness"] == PROVEN


def test_high_dollar_low_margin_fails(targets_5k_15):
    # $20k profit at 10% margin
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(
            proposed_bid_revenue=200000,
            product_acquisition=160000,
            outbound_freight=10000,
            financing_cost=10000,
        ),
        targets=targets_5k_15,
    )
    assert float(d["net_profit"]) == pytest.approx(20000, abs=0.02)
    assert float(d["net_margin"]) == pytest.approx(0.10, abs=1e-4)
    assert d["readiness"] == REJECT
    assert "HIGH_DOLLAR_CANNOT_WAIVE_MARGIN_FLOOR" in d["missing_requirements"]


def test_missing_freight_blocks_proven(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(outbound_freight=None, outbound_freight_status=None, outbound_freight_reason=None),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert any("FREIGHT" in m or "OUTBOUND" in m for m in d["missing_requirements"])


def test_missing_financing_blocks_proven(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(financing_cost=None, financing_status=None, financing_transaction_verified=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert any("FINANCING" in m for m in d["missing_requirements"])


def test_zero_financing_requires_explanation(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(
            financing_cost=0,
            financing_status="VERIFIED_ZERO",
            financing_reason=None,
            financing_transaction_verified=False,
            proposed_bid_revenue=30000,
            product_acquisition=18000,
            outbound_freight=2000,
        ),
        targets=targets_5k_15,
    )
    assert d["readiness"] != PROVEN
    assert "ZERO_FINANCING_REQUIRES_FUNDING_EXPLANATION" in d["missing_requirements"] or any(
        "FINANCING" in m for m in d["missing_requirements"]
    )


def test_zero_financing_with_reason_can_prove(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(
            financing_cost=0,
            financing_status="VERIFIED_ZERO",
            financing_reason="supplier_net30_covers_full_prepay_no_lender",
            financing_transaction_verified=True,
            proposed_bid_revenue=30000,
            product_acquisition=20000,
            outbound_freight=2000,
        ),
        targets=targets_5k_15,
    )
    assert float(d["net_profit"]) == pytest.approx(8000, abs=0.02)
    assert d["readiness"] == PROVEN


def test_historical_award_is_not_proposed_revenue(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(proposed_bid_revenue=None, historical_award_value=90000),
        targets=targets_5k_15,
    )
    assert d["proposed_bid_revenue"] is None
    assert d["historical_award_value"] == 90000.0
    assert d["readiness"] == VERIFY
    assert "PROPOSED_BID_REVENUE" in d["missing_requirements"]
    assert "HISTORICAL_AWARD_IS_NOT_PROPOSED_REVENUE" in d["missing_requirements"]


def test_rollup_adapter_does_not_use_historical_as_revenue(targets_5k_15, monkeypatch):
    monkeypatch.setenv("M3_MIN_EXPECTED_PROFIT_USD", "5000")
    lie = {
        "rollup": {
            "TOTAL_KNOWN_HISTORICAL_VALUE": 80000,
            "TOTAL_KNOWN_RETAIL_COST": 55000,
            "estimated_freight": 5000,
            "financing_cost": 4000,
            "freight_known": True,
            "coverage_pct": 100,
            "completeness_grade": "A",
            "line_count": 40,
            "lines_with_acquisition": 40,
        }
    }
    out = from_line_item_rollup(lie)
    assert out.get("proposed_bid_revenue") is None
    assert out.get("historical_award_value") == 80000.0
    assert out["readiness"] == VERIFY
    assert "PROPOSED_BID_REVENUE" in (out.get("missing_requirements") or out.get("missing_facts") or [])


def test_partial_line_coverage_blocks(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(line_count_required=40, line_count_sourced=39, sourcing_complete=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert "PARTIAL_LINE_SOURCING_COVERAGE" in d["missing_requirements"]


def test_unapproved_equivalent_blocks(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(equivalents_authorized=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert "UNAPPROVED_EQUIVALENT" in d["missing_requirements"]


def test_expired_quote_blocks(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(supplier_quote_expired=True),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert "SUPPLIER_QUOTE_EXPIRED" in d["missing_requirements"]


def test_insufficient_advance_blocks(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(financing_advance_sufficient=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] != PROVEN
    assert "INSUFFICIENT_FINANCING_ADVANCE" in d["missing_requirements"]


def test_uncovered_prepayment_blocks(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(uncovered_prepayment=True),
        targets=targets_5k_15,
    )
    assert d["readiness"] != PROVEN
    assert "UNCOVERED_PREPAYMENT" in d["missing_requirements"]


def test_personal_cash_fails(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(personal_cash_required_usd=500),
        targets=targets_5k_15,
    )
    assert d["readiness"] == REJECT
    assert "PERSONAL_CASH_REQUIRED" in d["missing_requirements"]


def test_lender_marketing_not_approval(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(lender_generic_marketing_only=True, financing_transaction_verified=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] != PROVEN
    assert "LENDER_MARKETING_NOT_TRANSACTION_APPROVAL" in d["missing_requirements"]


def test_financing_fees_reduce_profit(targets_5k_15):
    low = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(financing_cost=1000),
        targets=targets_5k_15,
    )
    high = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(financing_cost=3000),
        targets=targets_5k_15,
    )
    assert float(low["net_profit"]) - float(high["net_profit"]) == pytest.approx(2000, abs=0.02)


def test_missing_evidence_is_verify_not_proven(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(product_acquisition=None, product_acquisition_status=None),
        targets=targets_5k_15,
    )
    assert d["readiness"] == VERIFY
    assert d["owner_queue_eligible"] is False


def test_hard_exclusion_is_reject(targets_5k_15):
    d = evaluate_verified_commercial_decision(
        **_base_proven_kwargs(product_only=False),
        targets=targets_5k_15,
    )
    assert d["readiness"] == REJECT


def test_profit_first_adapter_null_financing_not_zero(targets_5k_15, monkeypatch):
    monkeypatch.setenv("M3_MIN_EXPECTED_PROFIT_USD", "5000")
    monkeypatch.setenv("M3_MIN_MARGIN_PCT", "15")
    out = compute_expected_profit(
        expected_revenue=30000,
        product_cost=18000,
        freight=2000,
        financing=None,
        completeness_pct=100,
        evidence_grade="A",
    )
    assert out.get("financing") is None
    assert out["readiness"] == VERIFY
    assert out["costs_complete"] is False
