"""Tests for evidence exhaustion + quote reserve admission."""

from __future__ import annotations

from evidence_exhaustion.models import BUILD, QUOTE_OUTREACH_RESERVE, RESEARCH_RETRYABLE
from evidence_exhaustion.quote_reserve import admit_to_quote_reserve, sensitivity_table
from evidence_exhaustion.routing import classify_product_routing
from evidence_exhaustion.owner_surface import owner_pipeline_view, should_surface_quote_reserve
from evidence_exhaustion.exhaust import compute_exhaustion_score


def test_build():
    assert BUILD == "20261004-m3-evidence-exhaustion-v1"


def test_routing_brand_or_equal():
    r = classify_product_routing(
        {"raw_description": "Rain Bird 5004 or equal rotor", "part_number": "5004"}
    )
    assert r["routing_class"] == "BRAND_OR_EQUAL"


def test_routing_specialty():
    r = classify_product_routing(
        {
            "manufacturer": "Cummins",
            "part_number": "5579409PX",
            "raw_description": "OEM fuel injector authorized dealer",
        }
    )
    assert r["routing_class"] == "SPECIALTY_OEM_NARROW_CHANNEL"


def test_reserve_blocked_if_not_exhausted():
    out = admit_to_quote_reserve(
        eligibility_ok=True,
        has_package=True,
        usable_identity=True,
        fatal_execution_blocker=False,
        revenue_or_value=True,
        time_remaining_ok=True,
        sourcing_path="OEM",
        exhaustion_flags={"manufacturer_search_exhausted": True},  # incomplete
        exhaustion_score=40,
        plausible_profit_if_quote_ok=True,
    )
    assert out["admitted"] is False
    assert out["status"] == RESEARCH_RETRYABLE
    assert out["premature"] is True


def test_reserve_admitted_only_when_exhausted():
    flags = {
        "manufacturer_search_exhausted": True,
        "distributor_search_exhausted": True,
        "reseller_search_exhausted": True,
        "direct_catalog_exhausted": True,
        "structured_data_exhausted": True,
        "alternate_domain_search_exhausted": True,
        "history_search_exhausted": True,
    }
    out = admit_to_quote_reserve(
        eligibility_ok=True,
        has_package=True,
        usable_identity=True,
        fatal_execution_blocker=False,
        revenue_or_value=True,
        time_remaining_ok=True,
        sourcing_path="DISTRIBUTOR",
        exhaustion_flags=flags,
        exhaustion_score=85,
        plausible_profit_if_quote_ok=True,
        channel={"suggested_quote_substatus": "DISTRIBUTOR_QUOTE_REQUIRED"},
    )
    assert out["admitted"] is True
    assert out["status"] == QUOTE_OUTREACH_RESERVE
    assert out["premature"] is False


def test_sensitivity_table():
    t = sensitivity_table(expected_gov_revenue=48000)
    assert t["ok"] is True
    assert t["maximum_acceptable_acquisition_cost"]["break_even"] == 48000.0
    assert t["maximum_acceptable_acquisition_cost"]["profit_5000"] == 43000.0


def test_owner_reserve_hidden_when_strong():
    assert should_surface_quote_reserve(strong_lead_count=15) is False
    assert should_surface_quote_reserve(strong_lead_count=3) is True
    view = owner_pipeline_view(
        [{"readiness": "LENDER_READY"} for _ in range(12)],
        reserve_cases=[{"opportunity_id": "x"} for _ in range(5)],
    )
    assert view["quote_outreach_reserve_collapsed"] is True
    assert view["quote_outreach_reserve_count"] == 5
    assert view["quote_outreach_reserve_surfaced"] is False
    assert "Quote Outreach Reserve: 5" in view["quote_outreach_reserve_label"]


def test_exhaustion_score():
    flags = {k: True for k in (
        "manufacturer_search_exhausted",
        "distributor_search_exhausted",
        "reseller_search_exhausted",
        "direct_catalog_exhausted",
        "structured_data_exhausted",
        "alternate_domain_search_exhausted",
        "history_search_exhausted",
        "best_price_search_ran",
        "multiple_candidates_sought",
    )}
    assert compute_exhaustion_score(flags, routing="COMMON_BROAD_CHANNEL") >= 90
