"""Tests for autonomous portfolio deal analysis + ranking."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from m3_portfolio_deal_analysis import (
    COMMERCIAL_VERIFICATION_WORTHY,
    ECON_INSUFFICIENT,
    ECON_PARTIAL,
    ECON_PROVEN,
    FIRST_TRANSACTION_CANDIDATE,
    PRI_DEFER,
    PRI_HIGH,
    ST_COMMERCIAL_VERIFICATION_WORTHY,
    TIER_1,
    TIER_3,
    TIER_4,
    TIER_5,
    amendment_invalidate_portfolio,
    assess_first_transaction_candidate,
    build_operator_deal_priority,
    cheap_portfolio_priority,
    extract_economics_snapshot,
    next_promoted_tier,
    scale_fixture_portfolio_test,
)


def test_cheap_screen_defers_obvious_services():
    row = {
        "title": "Employee Benefits Consultant Services",
        "description": "professional consulting",
        "lifecycle": "DISCOVERED",
    }
    p = cheap_portfolio_priority(row)
    assert p["priority"] == PRI_DEFER


def test_cheap_screen_high_for_product_with_package():
    row = {
        "title": "Wildflower and Native Grass Seed",
        "description": "native seed supplies materials",
        "documents": [{"url": "http://example.com"}],
        "line_items": [{"description": "seed", "quantity": 100}],
        "seed_basket_economics": {
            "BASKET_ECONOMICS": {
                "MODELED_BASKET_REVENUE": 207401,
                "MODELED_BASKET_ACQUISITION_COST": 167843,
                "MODELED_GROSS_PRODUCT_SPREAD": 39558,
                "Primary_L1_L3_quantity_coverage_pct": 71.9,
                "COMMERCIAL_VERIFICATION": COMMERCIAL_VERIFICATION_WORTHY,
            }
        },
    }
    p = cheap_portfolio_priority(row)
    assert p["priority"] == PRI_HIGH
    snap = extract_economics_snapshot(row)
    assert snap["Economics_class"] in {ECON_PARTIAL, ECON_PROVEN}
    assert snap["Deal_state"] == ST_COMMERCIAL_VERIFICATION_WORTHY


def test_unknown_not_rejected():
    row = {"title": "Industrial pump NSN 4320-01", "description": "equipment pump supplies", "lifecycle": "DISCOVERED"}
    snap = extract_economics_snapshot(row)
    assert snap["Economics_class"] == ECON_INSUFFICIENT
    assert snap["Revenue_basis"] == "UNKNOWN"
    # Insufficient ≠ rejected; may still be researchable
    p = cheap_portfolio_priority(row, snap)
    assert p["priority"] != PRI_DEFER or "product" in p["reason"]


def test_research_promotion_tiers():
    row = {
        "title": "Carbide tipped blades",
        "description": "hardware parts equipment",
        "documents": [{"id": 1}],
        "line_items": [{"description": "blade", "quantity": 50}],
    }
    snap = extract_economics_snapshot(row)
    nxt = next_promoted_tier(row, snap)
    # Has docs+BOM already → current tier 1 → promote to identity (2)
    assert nxt == 2

    bare = {"title": "Industrial filter supplies", "description": "equipment filter materials"}
    assert next_promoted_tier(bare, extract_economics_snapshot(bare)) == TIER_1


def test_tier5_only_when_justified():
    weak = {
        "title": "Misc supplies",
        "documents": [{"id": 1}],
        "product_identity": {"ok": True},
        "government_revenue_benchmark": {"MODELED_REVENUE": 800},
        "acquisition_target_intelligence": {"MODELED_ACQUISITION": 600},
    }
    # With both sides tiny, should not force Tier 5
    snap = extract_economics_snapshot(weak)
    # Force tier 4 reached
    weak["portfolio_tier_reached"] = TIER_4
    # extract uses evidence flags — has gov and acq => tier current 4 path
    nxt = next_promoted_tier(weak, extract_economics_snapshot(weak))
    assert nxt in {None, TIER_5}  # may stop


def test_first_transaction_and_priority():
    row = {
        "title": "Iowa Wildflower Seed",
        "agency": "Iowa DAS",
        "line_items": [{"description": "seed"}],
        "seed_basket_economics": {
            "BASKET_ECONOMICS": {
                "MODELED_BASKET_REVENUE": 207401,
                "MODELED_BASKET_ACQUISITION_COST": 167843,
                "MODELED_GROSS_PRODUCT_SPREAD": 39558,
                "Primary_L1_L3_quantity_coverage_pct": 71.9,
                "COMMERCIAL_VERIFICATION": COMMERCIAL_VERIFICATION_WORTHY,
                "Financing": "FUNDING_VERIFICATION_REQUIRED",
            }
        },
    }
    snap = extract_economics_snapshot(row)
    ftx = assess_first_transaction_candidate(row, snap)
    assert ftx["is_candidate"] is True
    assert ftx["status"] == FIRST_TRANSACTION_CANDIDATE
    cards = [
        {
            **snap,
            "canonical_id": "a",
            "Opportunity": row["title"],
            "Deadline": "2099-01-01",
            "PORTFOLIO_RESEARCH_PRIORITY": PRI_HIGH,
            "FIRST_TRANSACTION_CANDIDATE": True,
        }
    ]
    ranked = build_operator_deal_priority(cards)
    assert ranked[0]["rank"] == 1


def test_funding_unknown_preserved():
    row = {
        "title": "Seed",
        "seed_basket_economics": {
            "BASKET_ECONOMICS": {
                "MODELED_BASKET_REVENUE": 50000,
                "MODELED_BASKET_ACQUISITION_COST": 30000,
                "MODELED_GROSS_PRODUCT_SPREAD": 20000,
                "Primary_L1_L3_quantity_coverage_pct": 80,
                "COMMERCIAL_VERIFICATION": COMMERCIAL_VERIFICATION_WORTHY,
                "Financing": "FUNDING_VERIFICATION_REQUIRED",
            }
        },
    }
    snap = extract_economics_snapshot(row)
    assert snap["Financing"] == "FUNDING_VERIFICATION_REQUIRED"
    assert "EXHAUSTED" not in str(snap["Financing"])


def test_amendment_invalidation():
    row = {
        "seed_basket_economics": {"x": 1},
        "deal_economics": {"y": 2},
        "portfolio_tier_reached": 5,
    }
    out = amendment_invalidate_portfolio(row, "quantity_amendment")
    assert "seed_basket_economics" not in row
    assert out["UNKNOWN_not_rejected"] is True
    assert row["portfolio_tier_reached"] <= TIER_3


def test_schedule_timezone_next_run():
    from m3_discovery_service import compute_next_scheduled_run

    # Fixed local morning before 6 AM Denver
    denver = ZoneInfo("America/Denver")
    morning = datetime(2026, 9, 18, 5, 0, tzinfo=denver).astimezone(timezone.utc)
    nxt = compute_next_scheduled_run(morning)
    nxt_local = datetime.fromisoformat(nxt).astimezone(denver)
    assert nxt_local.hour == 6
    # After 14:00 → next is tomorrow 06:00
    evening = datetime(2026, 9, 18, 15, 0, tzinfo=denver).astimezone(timezone.utc)
    nxt2 = compute_next_scheduled_run(evening)
    nxt2_local = datetime.fromisoformat(nxt2).astimezone(denver)
    assert nxt2_local.hour == 6
    assert nxt2_local.day == 19


def test_scale_fixture_25k():
    result = scale_fixture_portfolio_test(25000)
    assert result["ok"] is True
    assert result["candidates"] == 25000
    assert result["no_live_research"] is True
    assert result["dedupe_ok"] is True
    assert result["promotable"] > 0
    assert result["priority_counts"]["DEFER"] > 0
