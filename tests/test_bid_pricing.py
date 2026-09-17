"""Focused tests — bid assembly + pricing intelligence."""

from __future__ import annotations

from datetime import timedelta

from application_clock import now_utc
from bid_pricing_engine import analyze_bid_pricing, authorize_pricing_research, reprice_after_change
from bid_pricing_invalidation import evaluate_price_freshness, invalidate_pricing_state
from draft_bid_assembly import assemble_draft_bid_package, populate_field, validate_draft_bid_package
from historical_gov_price_benchmark import build_historical_government_price_benchmark, observation_unit_price
from line_item_pricing import build_line_item_pricing
from msrp_intelligence import compute_msrp_intelligence
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
from pricing_scenarios import (
    break_even_and_max_costs,
    build_pricing_scenarios,
    commercial_verification_targets,
    sensitivity_analysis,
)
from transaction_economics import compute_transaction_economics, profit_floor_config


LINES = [
    {
        "line_id": "CLIN1",
        "description": "Dell Latitude 5540",
        "quantity": 40,
        "acquisition_unit_cost": 900,
        "acquisition_confidence": "VERIFIED_BINDING",
        "msrp": 1400,
    }
]


def test_msrp_vs_acquisition_distinction():
    m = compute_msrp_intelligence(line_items=LINES, acquisition_cost=36000, proposed_bid=50000)
    assert m["acquisition_is_not_msrp"] is True
    assert m["msrp_represented_as_acquisition_cost"] is False
    assert m["potential_gross_spread_is_not_profit"] is True
    assert m["basket_msrp"] == 56000.0
    assert m["acquisition_discount_from_msrp_pct"] is not None


def test_historical_exact_median_weighted_recency_outliers():
    obs = [
        {"unit_price": 100, "quantity": 10, "exact_product": True, "award_date": "2026-01-01"},
        {"unit_price": 110, "quantity": 10, "exact_product": True, "award_date": "2026-06-01"},
        {"unit_price": 120, "quantity": 20, "exact_product": True, "award_date": "2026-08-01"},
        {"unit_price": 1000, "quantity": 1, "exact_product": True, "award_date": "2025-01-01"},  # outlier
    ]
    b = build_historical_government_price_benchmark(obs)
    assert b["median"] is not None
    assert b["weighted_average"] is not None
    assert b["recency_weighted_estimate"] is not None
    assert b["accounts_for_recency"] is True
    assert b["exact_product_observation_count"] >= 3
    assert b["anomalies"]  # outlier flagged


def test_mixed_award_not_fake_unit_price():
    obs = [{"award_total": 500000, "mixed_order": True, "quantity": 10, "award_date": "2026-01-01"}]
    assert observation_unit_price(obs[0]) is None
    b = build_historical_government_price_benchmark(obs)
    assert b["number_of_observations"] == 0
    assert b["mixed_totals_as_unit_prices"] is False
    assert any(e["reason"].startswith("mixed") for e in b["exclusions"])


def test_weak_comparables_excluded():
    obs = [{"unit_price": 50, "quantity": 1, "match_quality": "WEAK_COMPARABLE"}]
    b = build_historical_government_price_benchmark(obs)
    assert b["weak_comparables_excluded"] >= 1


def test_unknown_cost_not_zero_and_profit():
    econ = compute_transaction_economics(
        government_bid_revenue=50000,
        product_acquisition_cost=30000,
        freight_cost=None,
        financing_cost=2000,
        acquisition_confidence="VERIFIED_BINDING",
        freight_confidence="UNKNOWN",
        financing_confidence="DEFENSIBLE_ESTIMATE",
    )
    assert econ["FreightCost"]["value"] is None
    assert econ["FreightCost"]["unknown_treated_as_zero"] is False
    assert econ["ExpectedNetTransactionProfit"]["value"] is None
    assert econ["status"] == "INCOMPLETE"

    ok = compute_transaction_economics(
        government_bid_revenue=50000,
        product_acquisition_cost=30000,
        freight_cost=1500,
        financing_cost=2000,
        transaction_expense=500,
        risk_allowance=1000,
        acquisition_confidence="VERIFIED_BINDING",
        freight_confidence="VERIFIED_BINDING",
        financing_confidence="DEFENSIBLE_ESTIMATE",
    )
    assert ok["ExpectedNetTransactionProfit"]["value"] == 15000.0
    assert ok["NetTransactionMargin"] is not None
    assert ok["profit_floor_flag"] in {"PROFIT_TARGET_MET", "PROFIT_TARGET_EXCEEDED"}


def test_line_item_allocation_reconciles():
    lip = build_line_item_pricing(
        line_items=[
            {"line_id": "A", "quantity": 10, "acquisition_unit_cost": 100, "acquisition_confidence": "VERIFIED_BINDING"},
            {"line_id": "B", "quantity": 5, "acquisition_unit_cost": 200, "acquisition_confidence": "VERIFIED_BINDING"},
        ],
        freight_total=300,
        financing_total=100,
        expense_total=50,
        risk_total=25,
    )
    assert lip["reconciliation"]["freight_reconciles"] is True
    assert lip["reconciliation"]["financing_reconciles"] is True
    assert lip["reconciliation"]["totals_reconcile_exactly"] is True
    assert lip["award_mode"] == "ALL_OR_NONE"


def test_partial_and_all_or_none_modes():
    a = build_line_item_pricing(line_items=LINES, freight_total=100, financing_total=100, award_mode="PARTIAL_AWARD")
    assert a["award_mode"] == "PARTIAL_AWARD"
    b = build_line_item_pricing(line_items=LINES, freight_total=100, financing_total=100, award_mode="LINE_ITEM_AWARD")
    assert b["award_mode"] == "LINE_ITEM_AWARD"


def test_scenarios_no_win_probability_and_insufficient():
    s = build_pricing_scenarios(
        acquisition=36000,
        freight=2000,
        financing=1500,
        risk=500,
        historical_benchmark_unit=1200,
        quantity=40,
        basket_msrp=56000,
        evaluation_basis="LPTA",
        acquisition_confidence="VERIFIED_BINDING",
        freight_confidence="DEFENSIBLE_ESTIMATE",
        financing_confidence="DEFENSIBLE_ESTIMATE",
    )
    assert len(s["scenarios"]) == 3
    assert s["win_probability_invented"] is False
    assert all(sc["win_probability"] is None for sc in s["scenarios"])
    assert s["recommendation_status"] == "RECOMMENDED_BID_PRICE"

    weak = build_pricing_scenarios(
        acquisition=None,
        freight=None,
        financing=None,
    )
    assert weak["recommendation_status"] == "BID_PRICE_REQUIRES_COMMERCIAL_VERIFICATION"


def test_profit_floor_config():
    cfg = profit_floor_config(minimum_transaction_profit=12000, minimum_transaction_margin=0.08)
    assert cfg["minimum_transaction_profit"] == 12000


def test_break_even_and_sensitivity_and_targets():
    be = break_even_and_max_costs(
        bid_revenue=50000,
        freight=2000,
        financing=1500,
        expense=500,
        risk=1000,
        acquisition=30000,
        target_profit=10000,
    )
    assert be["max_acquisition_for_target_profit"] == 35000.0
    assert be["minimum_acceptable_bid_revenue"] == 45000.0
    sens = sensitivity_analysis(
        base_bid=50000,
        acquisition=30000,
        freight=2000,
        financing=1500,
        expense=500,
        risk=1000,
    )
    assert len(sens["cases"]) == 3
    assert sens["acquisition_headroom_before_floor"] is not None
    targets = commercial_verification_targets(
        bid_revenue=50000,
        freight=2000,
        financing=1500,
        expense=500,
        risk=1000,
        acquisition=30000,
        delivery_by="2026-11-01",
        authorization_document="OEM letter",
    )
    assert targets["performed"] is False
    assert targets["outreach_count"] == 0
    assert any("supplier" in a["action"] for a in targets["actions"])
    assert any("financing" in a["action"] for a in targets["actions"])


def test_price_freshness_stale_blocks():
    fresh = evaluate_price_freshness(priced_at=now_utc().isoformat())
    assert fresh["state"] == "PRICE_CURRENT"
    stale = evaluate_price_freshness(priced_at=(now_utc() - timedelta(days=30)).isoformat())
    assert stale["state"] == "PRICE_STALE"
    assert stale["blocks_final_pricing_readiness"] is True


def test_invalidation_preserves_unrelated():
    state = {
        "conclusions": {
            "acquisition_cost": {"v": 1},
            "freight": {"v": 1},
            "bid_price": {"v": 1},
            "profit": {"v": 1},
            "unrelated_naics": {"ok": True},
        }
    }
    out = invalidate_pricing_state(state, change_type="QUANTITY_CHANGE")
    assert out["conclusions"]["freight"]["status"] == "INVALIDATED"
    assert out["conclusions"]["unrelated_naics"]["ok"] is True
    assert "unrelated_naics" in out["last_invalidation"]["preserved"]


def test_draft_population_no_signature_no_fabricate():
    draft = assemble_draft_bid_package(
        solicitation_id="S1",
        company_facts={"legal_name": "Acme LLC", "legal_name_verified": True, "uei": None},
        line_pricing={"lines": [{"line_id": "1", "extended_bid": 100}], "reconciliation": {"totals_reconcile_exactly": True}},
        recommended_bid={"total_bid": 100},
        required_forms=[{"form_key": "pricing_sheet", "requirement": "REQUIRED", "signature_required": True}],
        submission={"submission_method": "PORTAL", "deadline": "2026-10-01"},
        amendments_accounted=True,
    )
    assert draft["signatures_applied"] is False
    assert draft["certifications_asserted"] is False
    assert draft["bid_submitted"] is False
    assert draft["fabricated_fields"] is False
    assert draft["bidder_company_information"]["legal_name"]["status"] == "POPULATED_FROM_VERIFIED_DATA"
    assert draft["bidder_company_information"]["uei"]["status"] == "OPERATOR_INPUT_REQUIRED"
    unknown = populate_field(None, verified=False, field_name="x")
    assert unknown["fabricated"] is False


def test_draft_validation_blocks():
    draft = assemble_draft_bid_package(solicitation_id="S1")
    v = validate_draft_bid_package(draft, submission_method_known=False, deadline_actionable=False)
    assert v["passed"] is False
    assert "deadline_not_actionable" in v["blockers"]


def test_cost_governor_voi():
    denied = authorize_pricing_research(
        solicitation_id="S1",
        question="nice fluff",
        could_change_price=False,
    )
    assert denied["authorized"] is False
    reused = authorize_pricing_research(
        solicitation_id="S1",
        question="supplier cost?",
        reusable_evidence_sufficient=True,
    )
    assert reused["authorized"] is False


def test_full_analysis_and_reprice():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    analysis = analyze_bid_pricing(
        solicitation_id="IT-1",
        line_items=LINES,
        historical_observations=[
            {"unit_price": 1100, "quantity": 40, "exact_product": True, "award_date": "2026-05-01"},
            {"unit_price": 1150, "quantity": 20, "exact_product": True, "award_date": "2026-07-01"},
            {"unit_price": 1080, "quantity": 30, "exact_product": True, "award_date": "2026-08-01"},
        ],
        freight_cost=2500,
        freight_confidence="DEFENSIBLE_ESTIMATE",
        financing_cost=1800,
        financing_confidence="DEFENSIBLE_ESTIMATE",
        evaluation_basis="LPTA",
        submission={"submission_method": "PORTAL", "deadline": "2026-10-20 3:00 PM CDT", "timezone": "CDT"},
        company_facts={"legal_name": "Test Co", "legal_name_verified": True},
        required_forms=[{"form_key": "pricing_sheet", "requirement": "REQUIRED", "signature_required": True}],
        amendments_accounted=True,
        delivery_by="2026-11-15",
        paid_research_questions=["Will freight quote change bid?"],
    )
    assert analysis["kind"] == "BidPricingAnalysis"
    assert analysis["outreach"]["bids_submitted"] == 0
    assert analysis["win_probability_invented"] is False
    assert analysis["pricing_audit"]
    assert analysis["operator_summary"]["msrp"] is not None
    updated = [
        {**LINES[0], "quantity": 50},
    ]
    repriced = reprice_after_change(
        analysis,
        change_type="QUANTITY_CHANGE",
        updated_line_items=updated,
        freight_cost=3000,
        freight_confidence="DEFENSIBLE_ESTIMATE",
        financing_cost=2000,
        financing_confidence="DEFENSIBLE_ESTIMATE",
    )
    assert repriced["prior_invalidation"]["change_type"] == "QUANTITY_CHANGE"
    assert "unrelated_naics" in repriced["prior_invalidation"]["preserved"]


def test_api_pricing_routes(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    r = client.post(
        "/api/opportunities/PRICE-API-1/analyze-pricing",
        json={
            "line_items": LINES,
            "freight_cost": 2000,
            "freight_confidence": "DEFENSIBLE_ESTIMATE",
            "financing_cost": 1500,
            "financing_confidence": "DEFENSIBLE_ESTIMATE",
            "historical_observations": [
                {"unit_price": 1100, "quantity": 10, "exact_product": True, "award_date": "2026-06-01"},
            ],
            "submission": {"submission_method": "EMAIL"},
            "amendments_accounted": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "BidPricingAnalysis"
    assert client.get("/api/opportunities/PRICE-API-1/pricing").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/pricing-scenarios").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/transaction-economics").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/draft-bid").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/bid-package-manifest").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/commercial-verification-targets").status_code == 200
    assert client.get("/api/opportunities/PRICE-API-1/pricing-audit").status_code == 200


def test_idempotent_pricing_cache():
    from app import _BID_PRICING_CACHE, _bid_pricing_for

    _BID_PRICING_CACHE.clear()
    a = _bid_pricing_for("IDEM-P1", {"force": True, "line_items": LINES, "freight_cost": 1, "financing_cost": 1,
                                      "freight_confidence": "DEFENSIBLE_ESTIMATE", "financing_confidence": "DEFENSIBLE_ESTIMATE"})
    b = _bid_pricing_for("IDEM-P1", {})
    assert a["solicitation_id"] == b["solicitation_id"]
