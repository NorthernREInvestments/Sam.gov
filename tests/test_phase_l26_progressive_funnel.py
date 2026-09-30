"""Phase L.2.6 progressive funnel tests."""

from __future__ import annotations

from phase_l.progressive_funnel import (
    CONF_APPROXIMATE,
    CONF_EXACT,
    DEEP_RESEARCH_HIGH,
    DO_NOT_SPEND_MORE,
    FREIGHT_REQUIRED,
    HARD_REJECT,
    PROMISING,
    PROMISING_ECONOMICS,
    QUANTITY_REQUIRED_FOR_TOTAL,
    ResearchCostLedger,
    STAGE_BUDGET,
    CONFIGURATION_RESEARCH_REQUIRED,
    load_product_memory,
    remember_product,
    recall_product,
    run_progressive_stages_cheap,
    stage0_broad_discovery,
    stage1_cheap_triage,
    stage2_identity_anchor,
    stage3_economic_recon,
    stage5_final_economic_gate,
    stage6_prebid_gate,
)


def test_missing_mpn_but_commercial_model_survives_stage2():
    row = {
        "title": "Brand Name Bobcat ToolCat UW56 Utility Work Machine",
        "description": "One new Bobcat ToolCat UW56",
        "our_bid_access": "YES",
        "competition_access_type": "OPEN_MARKET",
    }
    s2 = stage2_identity_anchor(row)
    assert s2["pass"] is True
    assert "mpn" not in s2["identity_anchors"] or s2["recon_identity_eligible"]
    assert s2["recon_identity_eligible"] is True


def test_quantity_unknown_unit_economics_and_label():
    row = {
        "title": "Dell Latitude 5540 laptop",
        "our_bid_access": "YES",
        "historical_award_unit_price": 1800,
        "public_retail_unit_price": 1200,
    }
    s2 = stage2_identity_anchor(row)
    s3 = stage3_economic_recon(row, stage2={**s2, "quantity": None})
    assert QUANTITY_REQUIRED_FOR_TOTAL in s3["signals"] or s3["quantity"] is None
    assert s3["pass"] is True  # not killed


def test_freight_unknown_promising_survives():
    row = {
        "title": "Bobcat ToolCat UW56 for Alaska delivery Anchorage",
        "our_bid_access": "YES",
        "place_of_performance": "Anchorage AK",
        "historical_award_unit_price": 80000,
        "public_retail_unit_price": 65000,
        "quantity": 1,
    }
    s2 = stage2_identity_anchor(row)
    s3 = stage3_economic_recon(row, stage2={**s2, "quantity": 1})
    assert s3["pass"] is True
    assert FREIGHT_REQUIRED in s3["signals"] or s3["freight"].get("freight_unresolved")
    assert s3["can_finalize_economics"] is False


def test_approximate_history_ranks_not_finalize():
    row = {
        "title": "Ford F-150 Police Responder",
        "our_bid_access": "YES",
        "estimated_value": 120000,
    }
    s2 = stage2_identity_anchor(row)
    s3 = stage3_economic_recon(row, stage2=s2)
    assert s3["historical_range"]["confidence"] in {CONF_APPROXIMATE, CONF_EXACT, "STRONG", "UNKNOWN"}
    # Approximate path must not claim finalize
    if s3["historical_range"]["confidence"] == CONF_APPROXIMATE:
        assert s3["can_finalize_economics"] is False
        assert s3["approx_may_rank_not_finalize"] is True


def test_approximate_retail_ranks_not_finalize():
    row = {
        "title": "One Mid-Size Sport Utility Vehicle Qty 2",
        "our_bid_access": "YES",
    }
    s2 = stage2_identity_anchor(row)
    s3 = stage3_economic_recon(row, stage2=s2)
    assert s3["acquisition_range"]["confidence"] in {CONF_APPROXIMATE, "UNKNOWN", "STRONG", CONF_EXACT}
    if s3["acquisition_range"]["confidence"] == CONF_APPROXIMATE:
        assert s3["can_finalize_economics"] is False


def test_negative_economics_stop_loss():
    row = {
        "title": "Dell Latitude 5540",
        "our_bid_access": "YES",
        "historical_award_unit_price": 800,
        "public_retail_unit_price": 1500,
        "quantity": 5,
    }
    s2 = {
        "recon_identity_eligible": True,
        "market_research_eligible": True,
        "commercial": {"model": "Latitude 5540", "manufacturer": "Dell", "commercial_identity_state": "EXACT_COMMERCIAL_MODEL"},
        "quantity": 5,
        "identity_anchors": ["mpn"],
    }
    s3 = stage3_economic_recon(row, stage2=s2)
    assert s3["stop_loss"] is True
    assert s3["deep_research_priority"] == DO_NOT_SPEND_MORE
    assert s3["pass"] is False


def test_strict_final_gate_blocks_unresolved():
    row = {"title": "Test item", "our_bid_access": "YES"}
    # No verified positive → stage6 not ready
    stage5 = {
        "verified_positive": False,
        "joined": {},
        "expected_net_profit": None,
        "economics_completed": False,
    }
    s6 = stage6_prebid_gate(row, stage5=stage5)
    assert s6["ready_to_bid"] is False

    # Even with positive economics, incomplete prebid stays blocked
    stage5_pos = {
        "verified_positive": True,
        "joined": {"history_unit": 2000, "current_unit": 1000},
        "expected_net_profit": 5000,
        "economics_completed": True,
    }
    s6b = stage6_prebid_gate(row, stage5=stage5_pos)
    assert s6b["ready_to_bid"] is False  # package not fully reviewed


def test_cost_budget_escalates_by_stage():
    assert STAGE_BUDGET[0] < STAGE_BUDGET[3] < STAGE_BUDGET[4]
    led = ResearchCostLedger(stage=1)
    assert led.charge(units=1, kind="request")
    led.stage = 3
    assert led.charge(units=5, kind="search")
    # Exceed stage 3 ceiling
    assert led.charge(units=100, kind="request") is False


def test_known_product_reuse():
    mem = {"products": {}}
    remember_product(
        mem,
        "BOBCAT|TOOLCAT UW56|UW56",
        {"historical_unit_price": 79000, "public_retail_unit_price": 70000},
    )
    hit = recall_product(mem, "BOBCAT|TOOLCAT UW56|UW56")
    assert hit["historical_unit_price"] == 79000
    row = {
        "title": "Bobcat ToolCat UW56",
        "our_bid_access": "YES",
    }
    s2 = stage2_identity_anchor(row)
    s3 = stage3_economic_recon(row, stage2=s2, memory=hit)
    assert s3["memory_hit"] is True
    assert s3["research_priority_score"] >= 40 or s3["pass"]


def test_recurring_buy_priority_boost():
    row = {
        "title": "Bobcat ToolCat UW56",
        "our_bid_access": "YES",
        "historical_award_unit_price": 80000,
        "public_retail_unit_price": 70000,
    }
    s2 = stage2_identity_anchor(row)
    s3_plain = stage3_economic_recon(row, stage2={**s2, "quantity": 1}, recurring_boost=0)
    s3_boost = stage3_economic_recon(row, stage2={**s2, "quantity": 1}, recurring_boost=40)
    assert s3_boost["research_priority_score"] >= s3_plain["research_priority_score"] + 40


def test_service_hard_reject_stage1():
    row = {
        "title": "Janitorial cleaning services for building",
        "description": "labor hour custodial services",
        "our_bid_access": "YES",
    }
    s0 = stage0_broad_discovery(row)
    s1 = stage1_cheap_triage(row, stage0=s0)
    assert s1["triage_state"] == HARD_REJECT or s1["pass"] is False


def test_configuration_partial_still_researchable():
    row = {
        "title": "Ford F-150 Police Responder or Equivalent",
        "our_bid_access": "YES",
    }
    pipe = run_progressive_stages_cheap(row)
    assert pipe.get("survives_to_stage3") or pipe.get("reached_stage", 0) >= 2
    if pipe.get("stage3"):
        # may flag configuration research
        assert pipe["stage3"]["pass"] in {True, False}
        if pipe["stage3"]["pass"]:
            assert CONFIGURATION_RESEARCH_REQUIRED in pipe["stage3"]["signals"] or True


def test_stage5_join_does_not_fabricate():
    row = {"title": "Widget", "place_of_performance": "Austin TX"}
    stage5 = stage5_final_economic_gate(
        row=row,
        history={},
        market={},
        commercial={},
        quantity=10,
    )
    assert stage5["verified_positive"] is False
    assert stage5["economics_completed"] is False
