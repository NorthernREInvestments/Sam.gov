"""Visibility: unknown pricing must not hide eligible product opportunities."""

from __future__ import annotations

from buyer_intelligence.available_deals_visibility_v1 import (
    BLOCKED,
    WORTH,
    classify_eligibility,
    classify_record,
    research_priority_score,
)


def test_hubzone_only_is_ineligible():
    rec = {
        "title": "Rotor bolt tool kit",
        "set_aside": "HUBZone Set-Aside",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-12-01T00:00:00+00:00",
    }
    elig = classify_eligibility(rec)
    assert elig["eligible"] is False
    row = classify_record("oid-hub", rec)
    assert row["visibility_bucket"] == BLOCKED


def test_total_sb_product_unknown_pricing_is_worth_researching():
    rec = {
        "title": "Paper towels and toilet tissue supply",
        "set_aside": "Total Small Business Set-Aside",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-12-01T00:00:00+00:00",
        "buyer": "City of Test",
        "current_funnel_state": "RAW",
    }
    row = classify_record("oid-paper", rec)
    assert row["visibility_bucket"] == WORTH
    assert row["pricing_status"] == "UNKNOWN"
    assert "MISSING_PRICING_STILL_VISIBLE" in row["hide_reasons_avoided"]
    assert row["research_priority_score"] > 0


def test_unknown_profit_does_not_zero_priority():
    rec = {
        "title": "LED lighting supplies",
        "set_aside": "Unrestricted",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-11-01T00:00:00+00:00",
    }
    elig = classify_eligibility(rec)
    score = research_priority_score(rec, elig, {})
    assert score > 50


def test_service_contract_blocked():
    rec = {
        "title": "Janitorial service for city hall",
        "set_aside": "Unrestricted",
        "product_service_classification": "PURE_SERVICE",
        "deadline": "2099-12-01T00:00:00+00:00",
    }
    row = classify_record("oid-svc", rec)
    assert row["visibility_bucket"] == BLOCKED


def test_missing_set_aside_is_unknown_but_visible():
    """Unknown eligibility must not be assumed eligible; still Worth Researching."""
    rec = {
        "title": "COMPRESSOR TOOL KIT",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-11-12T16:30:00-04:00",
        "typeOfSetAside": "",
    }
    elig = classify_eligibility(rec)
    assert elig["eligible"] is None
    row = classify_record("oid-ba25", rec)
    assert row["visibility_bucket"] == WORTH
    assert row["eligibility_bucket"] == "UNKNOWN_VERIFY"


def test_hzc_code_blocks():
    rec = {
        "title": "51--TOOL KIT,ROTOR BOLT",
        "typeOfSetAside": "HZC",
        "typeOfSetAsideDescription": "Historically Underutilized Business (HUBZone) Set-Aside (FAR 19.13)",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-10-15",
    }
    row = classify_record("oid-hzc", rec)
    assert row["visibility_bucket"] == BLOCKED
    assert row["eligible"] is False


def test_bidnet_open_portal_eligible_without_set_aside():
    rec = {
        "title": "Paper and janitorial supplies",
        "platform": "live_bidnet",
        "jurisdiction": "STATE_LOCAL",
        "is_federal": False,
        "product_service_classification": "TANGIBLE_PRODUCT",
        "deadline": "2099-12-01T00:00:00+00:00",
    }
    elig = classify_eligibility(rec)
    assert elig["eligible"] is True
    row = classify_record("oid-bn", rec)
    assert row["visibility_bucket"] == WORTH
    assert "Verify set-aside" not in (row.get("next_action") or "")
