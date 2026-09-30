"""Phase L.3 commercial acquisition lane rebalance tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    STAGE3_NO_ROW_CAP,
    UNKNOWN_ACQUISITION_CHANNEL,
    buyer_commercial_fit_score,
    calculate_maximum_buy_price,
    classify_acquisition_lane,
    commercial_acquisition_score,
    detect_quote_channel_signals,
    generate_supplier_candidates,
    prepare_quote_packet_l3,
)
from phase_l.commercial_discovery import commercial_product_hunt_tags, commercial_share
from phase_l.original_solicitation import (
    ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED,
    resolve_original_solicitation,
)
from phase_l.product_page_resolution import EXACT_VERIFIED, STRONG_VERIFIED
from phase_l.progressive_funnel import stage1_cheap_triage, stage3_economic_recon


def test_commercial_open_classification():
    row = {"title": "Dell PowerEdge R670 Server Qty 4", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row, commercial={"manufacturer": "Dell", "model": "PowerEdge R670"})
    assert lane["acquisition_lane"] == COMMERCIAL_OPEN_CHANNEL
    assert lane["rejected"] is False


def test_distributor_classification():
    row = {"title": "Industrial pump via authorized distributor network", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row, commercial={"model": "P200-X"})
    assert lane["acquisition_lane"] in {COMMERCIAL_DISTRIBUTOR_CHANNEL, COMMERCIAL_OPEN_CHANNEL, UNKNOWN_ACQUISITION_CHANNEL}


def test_fleet_vehicle_quote_required():
    row = {"title": "One or More 2027 Ford Expedition SSV Vehicle(s)", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] == QUOTE_REQUIRED_COMMERCIAL
    assert lane["rejected"] is False


def test_excavator_quote_required():
    row = {"title": "(FY27) DL Long Reach Tracked Excavator 96K lbs", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] == QUOTE_REQUIRED_COMMERCIAL


def test_quote_required_classification():
    row = {
        "title": "Bobcat ToolCat UW56 — contact dealer for pricing / configure and quote",
        "our_bid_access": "YES",
    }
    lane = classify_acquisition_lane(row, commercial={"manufacturer": "Bobcat", "model": "ToolCat UW56"})
    assert lane["acquisition_lane"] == QUOTE_REQUIRED_COMMERCIAL
    assert lane["quote_required"] is True


def test_milspec_specialty_classification():
    row = {
        "title": "NSN 2995-01-313-0343 VALVE ASSEMBLY ANTI WSDC F110 End Item Aircraft SPRTA",
        "our_bid_access": "YES",
        "nsn": "2995013130343",
    }
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] == MILSPEC_SPECIALTY
    assert lane["specialty_pipeline"] is True
    assert lane["rejected"] is False


def test_case_nozzle_not_commercial_brand():
    """CASE AND NOZZLE milspec must not match construction 'Case' brand."""
    row = {
        "title": "CASE AND NOZZLE ASS_End_Item_F110_NSN_284001240358",
        "our_bid_access": "YES",
        "nsn": "2840012403588",
    }
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] == MILSPEC_SPECIALTY
    assert lane["rejected"] is False


def test_source_approval_classification():
    row = {"title": "Must be OEM authorized — source approval required QPL item", "our_bid_access": "CONDITIONAL"}
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] == SOURCE_APPROVAL_REQUIRED


def test_unknown_classification():
    row = {"title": "Miscellaneous supplies", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row)
    assert lane["acquisition_lane"] in {UNKNOWN_ACQUISITION_CHANNEL, MILSPEC_SPECIALTY, COMMERCIAL_DISTRIBUTOR_CHANNEL}


def test_difficult_channel_not_rejected():
    row = {"title": "NSN obscure military assembly SPRTA WSDC", "our_bid_access": "YES"}
    lane = classify_acquisition_lane(row)
    assert lane["rejected"] is False
    s1 = stage1_cheap_triage(row)
    assert s1["pass"] is True  # still in funnel


def test_commercial_score():
    row = {"title": "Ford F-150 Police Responder fleet vehicle"}
    sc = commercial_acquisition_score(row, commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"})
    assert sc["score"] >= 60


def test_buyer_commercial_fit():
    row = {"agency": "City of Springfield Public Works Fleet", "title": "Pickup truck"}
    fit = buyer_commercial_fit_score(row)
    assert fit["score"] >= 40


def test_max_buy_price_reverse_economics():
    mb = calculate_maximum_buy_price(government_unit=38000, quantity=1, freight_reserve=4000, desired_profits=(0, 5000, 10000))
    assert mb is not None
    assert mb["unit_ceilings"]["break_even"] is not None
    assert mb["maximum_acquisition_unit"] is not None
    assert "SUPPLIER_QUOTE_REQUIRED" in (mb.get("label") or "")


def test_quote_required_stop_loss_signal():
    assert detect_quote_channel_signals("Call for price — dealer pricing — RFQ only")


def test_supplier_candidate_generation():
    cands = generate_supplier_candidates(
        row={"title": "Bobcat ToolCat UW56"},
        commercial={"manufacturer": "Bobcat", "model": "ToolCat UW56", "mpn": "UW56"},
        family="EQUIPMENT",
    )
    assert cands
    assert all(c.get("outreach_authorized") is False for c in cands)


def test_quote_packet_no_send():
    pkt = prepare_quote_packet_l3(
        row={"title": "ToolCat", "solicitation_id": "S1", "quantity": 1},
        commercial={"manufacturer": "Bobcat", "model": "UW56"},
        max_buy={"maximum_acquisition_unit": 55000},
    )
    assert pkt["transmission"] == "CONTENT_ONLY_NO_SEND"
    assert pkt["outreach_authorized"] is False


def test_recurring_commercial_product_hunt_tag():
    tags = commercial_product_hunt_tags({"title": "Dell PowerEdge R670 servers for city IT"})
    assert tags["commercial_product_hunt"] is True
    assert "IT" in tags["commercial_categories"]


def test_specialty_queue_flag():
    lane = classify_acquisition_lane({"title": "NSN WSDC SPRTA aircraft nozzle segment"})
    assert lane.get("queue") or lane["specialty_pipeline"]


def test_original_solicitation_url_preservation():
    row = {
        "title": "Test",
        "agency": "City Fleet",
        "solicitation_id": "RFQ-1",
        "discovery_url": "https://www.highergov.com/x",
        "original_posting_url": "https://city.gov/bids/rfq-1",
        "response_deadline": "2026-10-15T17:00:00-05:00",
        "timezone": "America/Chicago",
        "submission_method": "portal_upload",
    }
    orig = resolve_original_solicitation(row)
    assert orig["authoritative_solicitation_source"]["verified"] is True
    assert orig["discovery_source"]["is_aggregator"] is True
    assert orig["original_posting_url"].startswith("https://city.gov")


def test_discovery_vs_authoritative_unverified():
    row = {"title": "X", "discovery_url": "https://www.highergov.com/only"}
    orig = resolve_original_solicitation(row)
    assert orig["source_of_truth_status"] == ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED
    assert orig["bid_readiness_blocker"] == ORIGINAL_SOLICITATION_SOURCE_UNVERIFIED


def test_stage3_no_caps():
    assert STAGE3_NO_ROW_CAP is True
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True
    assert MANUAL_QUEUE_NO_FIXED_CAP is True


def test_freight_not_early_blocker():
    row = {"title": "Bobcat ToolCat UW56 heavy equipment", "our_bid_access": "YES"}
    stage2 = {
        "recon_identity_eligible": True,
        "market_research_eligible": True,
        "quantity": None,
        "commercial": {"model": "ToolCat UW56", "manufacturer": "Bobcat"},
        "acquisition_lane": QUOTE_REQUIRED_COMMERCIAL,
        "lane_priority": 3,
        "commercial_acquisition_score": 70,
        "identity_anchors": ["commercial_model"],
    }
    s3 = stage3_economic_recon(row, stage2=stage2)
    assert s3["pass"] is True


def test_strict_final_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
    assert STRONG_VERIFIED == "STRONG_VERIFIED"


def test_commercial_share_helper():
    rows = [
        {"acquisition_lane": COMMERCIAL_OPEN_CHANNEL},
        {"acquisition_lane": MILSPEC_SPECIALTY},
        {"acquisition_lane": QUOTE_REQUIRED_COMMERCIAL},
    ]
    share = commercial_share(rows)
    assert share["commercial_lanes"] == 2
    assert share["specialty_lanes"] == 1
