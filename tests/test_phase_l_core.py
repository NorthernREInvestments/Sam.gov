"""Phase L targeted regression tests — access, competition, economics, normalize."""

from __future__ import annotations

from phase_l.access_gate import (
    ACCESS_CONDITIONAL,
    ACCESS_NO,
    ACCESS_UNKNOWN,
    ACCESS_YES,
    BPA_ONLY,
    OPEN_MARKET,
    SOLE_SOURCE,
    TOTAL_SMALL_BUSINESS,
    VEHICLE_ONLY,
    classify_competition_access_type,
    evaluate_phase_l_access,
)
from phase_l.competition import (
    PRE_FILTERED_COMPETITION,
    VERY_LOW_OPEN_COMPETITION,
    annotate_competition,
    effective_competition_signal,
)
from phase_l.economics import (
    TIER_MONSTER,
    TIER_PASS,
    TIER_STRONG,
    build_phase_l_economics,
    profit_tier,
)
from phase_l.normalize import (
    READY_FOR_OWNER_REVIEW,
    infer_source_level,
    normalize_opportunity,
    owner_decision_state,
)


def test_vehicle_only_is_not_accessible():
    row = {
        "title": "RFOP BOAST BOA holders only — monitors",
        "description": "Limited to current BOAST BOA holders only.",
    }
    access = evaluate_phase_l_access(row)
    assert access["competition_access_type"] in {VEHICLE_ONLY, "BOAST_BOA"} or access[
        "competition_access_type"
    ] in {VEHICLE_ONLY, BPA_ONLY} or "BOAST" in str(access.get("access_blocker") or "")
    assert access["our_bid_access"] == ACCESS_NO
    assert access["actionable"] is False


def test_bpa_only_classification():
    text = "This order is restricted to BPA holders only."
    assert classify_competition_access_type({"title": "Toner", "description": text}) == BPA_ONLY


def test_sole_source_blocks():
    access = evaluate_phase_l_access(
        {"title": "Sole source HATZ engine", "description": "Sole-source to HATZ Diesel only."}
    )
    assert access["competition_access_type"] == SOLE_SOURCE
    assert access["our_bid_access"] == ACCESS_NO


def test_small_business_with_default_sb_is_yes():
    """COMPANY_CERTIFICATIONS default includes SB — Total SB set-aside is accessible."""
    access = evaluate_phase_l_access(
        {
            "title": "Tool kit",
            "description": "Total Small Business Set-Aside in accordance with FAR 19.5",
            "set_aside": "SBA",
        }
    )
    assert access["competition_access_type"] == TOTAL_SMALL_BUSINESS
    assert access["our_bid_access"] == ACCESS_YES
    assert access["can_compete"] is True


def test_small_business_other_than_small_is_no():
    access = evaluate_phase_l_access(
        {
            "title": "Tool kit",
            "description": "Total Small Business Set-Aside in accordance with FAR 19.5",
            "set_aside": "SBA",
        },
        profile={
            "sam_active": "UNKNOWN",
            "sba_size_status": "OTHER_THAN_SMALL",
            "set_aside_qualifications": [],
            "vehicles_held": [],
            "approved_sources": [],
        },
    )
    assert access["our_bid_access"] == ACCESS_NO
    assert "not_small_business" in str(access.get("access_blocker") or "")


def test_open_market_unrestricted_is_yes():
    """Phase L.1: empty SAM/profile must not force CONDITIONAL on open competition."""
    access = evaluate_phase_l_access(
        {
            "title": "IFB for commercial LED fixtures",
            "description": "Invitation for Bid. Full and open competition. Unrestricted.",
        }
    )
    assert access["competition_access_type"] in {OPEN_MARKET, "UNRESTRICTED"}
    assert access["our_bid_access"] == ACCESS_YES
    assert access["can_compete"] is True
    assert access["actionable"] is True


def test_easy_vendor_registration_keeps_yes_and_economics():
    """Invariant: ordinary state/local signup → YES + registration action; economics allowed."""
    access = evaluate_phase_l_access(
        {
            "title": "City printers IFB",
            "description": (
                "Invitation for Bid. Unrestricted full and open competition. "
                "Vendor registration required prior to bid submission."
            ),
            "source_id": "agency_city_austin_tx",
            "source_level": "LOCAL",
            "buyer_type": "LOCAL",
        }
    )
    assert access["our_bid_access"] == ACCESS_YES
    assert access["vendor_registration_required"] is True
    assert access["is_easy_registration"] is True
    assert access["registration_action"] in {
        "REGISTER_BEFORE_BID",
        "REGISTER_NOW",
        "REGISTER_NOW_RECURRING_BUYER",
    }
    assert access["submission_readiness"] == "REGISTRATION_PENDING"
    # Economics must be allowed to run (not gated by easy registration)
    econ = build_phase_l_economics(
        quantity=200,
        uom="EA",
        historical_unit_price=200.0,
        public_retail_unit_price=100.0,
        freight=500.0,
    )
    assert econ["expected_net_profit"] is not None
    assert econ["meets_floor"] is True
    norm = normalize_opportunity(
        {
            "title": "City printers IFB",
            "description": "Invitation for Bid. Unrestricted. Vendor registration required.",
            "source_id": "agency_city_austin_tx",
            "buyer_type": "LOCAL",
            "status": "OPEN",
            "live_status": "OPEN",
            "is_product": True,
            "quantity": 200,
            "uom": "EA",
            "historical_award_unit_price": 200.0,
            "public_retail_unit_price": 100.0,
            "freight": 500.0,
            "nsn": "7025-01-111-2222",
        },
        access=access,
        economics=econ,
    )
    assert norm["our_bid_access"] == ACCESS_YES
    assert norm["meets_floor"] is True
    assert owner_decision_state(norm) == READY_FOR_OWNER_REVIEW


def test_registration_vs_runway_blocks():
    access = evaluate_phase_l_access(
        {
            "title": "City printers IFB",
            "description": "Vendor registration required prior to bid submission.",
        },
        vendor_registration_lead_time_days=6,
        runway_days=8,
    )
    assert access["our_bid_access"] == ACCESS_NO
    assert "registration_lead_time_exceeds_runway" in str(access.get("access_blocker") or "")


def test_local_preference_does_not_auto_reject():
    access = evaluate_phase_l_access(
        {
            "title": "County MRO supplies",
            "description": "Invitation for Bid. Unrestricted. 5% local preference applies.",
        }
    )
    assert access["local_preference_exists"] is True
    assert access["local_preference_pct"] == 5.0
    assert access["our_bid_access"] != ACCESS_NO or "resident" in str(access.get("access_blocker") or "")


def test_prefiltered_competition_not_attractive():
    sig = effective_competition_signal(
        historical_offers_received=2,
        historical_competition_type=BPA_ONLY,
    )
    assert sig == PRE_FILTERED_COMPETITION
    ann = annotate_competition(
        historical_offers_received=2,
        competition_access_type=BPA_ONLY,
    )
    assert ann["competition_rank_score"] == 0
    assert ann["is_open_comparable"] is False


def test_open_low_offers_attractive():
    sig = effective_competition_signal(
        historical_offers_received=1,
        historical_competition_type=OPEN_MARKET,
    )
    assert sig == VERY_LOW_OPEN_COMPETITION
    ann = annotate_competition(
        historical_offers_received=1,
        competition_access_type=OPEN_MARKET,
    )
    assert ann["competition_rank_score"] == 100


def test_economics_retail_baseline_and_tiers():
    # 100 units * $200 hist bid - $150 retail - 5% fin = spread works
    econ = build_phase_l_economics(
        quantity=100,
        uom="EA",
        historical_unit_price=200.0,
        public_retail_unit_price=150.0,
        public_retail_source="example.com",
        freight=200.0,
        financing_rate=0.05,
    )
    assert econ["expected_revenue"] == 20000.0
    assert econ["public_retail_total"] == 15000.0
    assert econ["gross_retail_spread"] == 5000.0
    # financing 5% of 15000 = 750; net = 5000 - 200 - 750 = 4050 → FAIL floor
    assert econ["profit_tier"] == "FAIL"
    assert econ["meets_floor"] is False

    econ2 = build_phase_l_economics(
        quantity=200,
        uom="EA",
        historical_unit_price=200.0,
        public_retail_unit_price=100.0,
        freight=500.0,
    )
    # rev 40000 - retail 20000 - freight 500 - fin 1000 = 18500 → PASS
    assert econ2["expected_net_profit"] == 18500.0
    assert econ2["profit_tier"] == TIER_PASS

    econ3 = build_phase_l_economics(
        quantity=500,
        historical_unit_price=300.0,
        public_retail_unit_price=150.0,
        freight=1000.0,
    )
    # rev 150000 - 75000 - 1000 - 3750 = 70250 → EXCEPTIONAL-ish
    assert econ3["expected_net_profit"] >= 50000
    assert profit_tier(120000) == TIER_MONSTER
    assert profit_tier(30000) == TIER_STRONG


def test_discount_assumption_does_not_create_pass():
    """Failing retail baseline must not be rescued by imaginary discounts."""
    econ = build_phase_l_economics(
        quantity=10,
        historical_unit_price=100.0,
        public_retail_unit_price=95.0,
    )
    assert econ["meets_floor"] is False
    assert "financing_scenarios" in econ


def test_normalize_blocks_ready_without_access_yes():
    row = {
        "title": "BPA holders only laptops",
        "description": "Restricted to BPA holders only",
        "source_id": "fed_sam_public_search",
        "buyer_type": "FEDERAL",
        "status": "OPEN",
        "is_product": True,
        "quantity": 50,
        "uom": "EA",
        "historical_award_unit_price": 500,
        "public_retail_unit_price": 200,
    }
    norm = normalize_opportunity(row)
    assert norm["our_bid_access"] == ACCESS_NO
    assert norm["actionable_state"] != READY_FOR_OWNER_REVIEW
    assert owner_decision_state(norm) == "ACCESS_BLOCKED"


def test_infer_source_levels():
    assert infer_source_level({"source_id": "state_tx", "kind": "STATE"}) == "STATE"
    assert infer_source_level({"source_id": "coop_sourcewell_live"}) == "COOPERATIVE"
    assert infer_source_level({"source_id": "fed_sam_public_search", "buyer_type": "FEDERAL"}) == "FEDERAL"
    assert infer_source_level({"source_id": "agency_city_houston_tx", "kind": "LOCAL"}) == "LOCAL"


def test_api_failure_does_not_equal_access_no():
    """Missing API key / degraded discovery is not an access blocker by itself."""
    access = evaluate_phase_l_access(
        {
            "title": "Commercial UPS systems IFB",
            "description": "Invitation for Bid unrestricted full and open competition",
            "source_id": "fed_sam_public_search",
        }
    )
    assert "SAM_API" not in str(access.get("access_blocker") or "")
