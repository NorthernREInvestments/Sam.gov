"""Competitive intelligence — competition, incumbent, first-deal, category, financeability."""

from __future__ import annotations

from m3_competitive_intelligence import (
    BUCKET_A,
    FIN_EASY,
    MARKET_INCUMBENT,
    MARKET_OPEN,
    PRIORITY_HIGH,
    PROFILE_HIGH,
    PROFILE_LOW,
    PROFILE_UNKNOWN,
    analyze_competitive_top_opportunities,
    build_competition_profile,
    build_competitive_intelligence,
    build_financeability_profile,
    build_incumbent_analysis,
    build_new_entrant_advantage,
    category_key,
    deal_room_competitive_section,
    score_first_deal,
)
from m3_commercial_engine import SCORE_HIGH
from m3_pipeline_store import M3PipelineStore


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "comp-1"),
        "title": "Cisco Systems Network Switches",
        "description": "Supply of network switches delivery only",
        "agency": "State of Illinois",
        "product_category": "IT_NETWORKING",
        "estimated_value": 75000,
        "supplier_intelligence": {
            "Product": {
                "Manufacturer": "Cisco",
                "Model": "Network Switches",
                "identity_status": "COMPLETE",
                "sufficient_for_pricing_research": True,
                "Part_number": "UNKNOWN",
                "NSN": "UNKNOWN",
            },
            "Supply_chain": {"supplier_confidence": SCORE_HIGH, "all_channels": [{"company": "CDW"}, {"company": "SHI"}, {"company": "Insight"}]},
            "FIRST_DEAL_FIT": {"band": SCORE_HIGH, "FIRST_DEAL_FIT_SCORE": 85},
        },
        "commercial_intelligence": {
            "COMMERCIAL_OPPORTUNITY_SCORE": "MEDIUM",
            "Supply_Confidence": SCORE_HIGH,
            "Financing_Difficulty": "EASY",
        },
    }
    base.update(kwargs)
    return base


def test_competition_fragmented_is_low_concern_not_reject_on_bidders():
    row = _opp(
        historical_bidder_count=10,
        historical_awards=[
            {"winner": "Alpha Supply", "amount": 50000},
            {"winner": "Beta Distribution", "amount": 48000},
            {"winner": "Gamma Reseller", "amount": 51000},
            {"winner": "Alpha Supply", "amount": 49000},
        ],
    )
    comp = build_competition_profile(row)
    assert comp["historical_bidder_count"] == 10
    assert comp["unique_winners"] >= 3
    assert comp["profile"] in {PROFILE_LOW, "MEDIUM"}
    assert "bidder_count_alone_never_rejects" in comp["notes"]
    assert any("not_auto_reject" in r for r in comp["reasons"])


def test_incumbent_dominated_market():
    row = _opp(
        historical_awards=[
            {"winner": "Locked Inc", "amount": 100000},
            {"winner": "Locked Inc", "amount": 110000},
            {"winner": "Locked Inc", "amount": 105000},
            {"winner": "Locked Inc", "amount": 99000},
        ]
    )
    comp = build_competition_profile(row)
    assert comp["profile"] == PROFILE_HIGH
    inc = build_incumbent_analysis(row, comp)
    assert inc["market_structure"] == MARKET_INCUMBENT
    assert inc["INCUMBENT_RISK_SCORE"] >= 65


def test_open_market_low_incumbent_risk():
    row = _opp(
        historical_awards=[
            {"winner": "A Co", "amount": 10},
            {"winner": "B Co", "amount": 11},
            {"winner": "C Co", "amount": 12},
            {"winner": "D Co", "amount": 13},
        ]
    )
    comp = build_competition_profile(row)
    inc = build_incumbent_analysis(row, comp)
    assert comp["profile"] == PROFILE_LOW
    assert inc["market_structure"] == MARKET_OPEN
    assert inc["INCUMBENT_RISK_SCORE"] <= 30


def test_new_entrant_and_first_deal_scoring():
    row = _opp()
    comp = build_competition_profile(row)
    inc = build_incumbent_analysis(row, comp)
    adv = build_new_entrant_advantage(row, competition=comp, incumbent=inc)
    fin = build_financeability_profile(row)
    fd = score_first_deal(row, competition=comp, incumbent=inc, advantage=adv, finance=fin)
    assert adv["NEW_ENTRANT_ADVANTAGE_SCORE"] >= 40
    assert fin["classification"] in {FIN_EASY, "MODERATE"}
    assert fd["FIRST_DEAL_SCORE"] >= 40
    pkg = build_competitive_intelligence(row)
    assert pkg["BUCKET"] in {BUCKET_A, "BUCKET_B_GROWTH_TARGET", "BUCKET_C_WATCH_LIST", "BUCKET_D_LOW_PRIORITY"}


def test_no_history_unknown_not_auto_reject():
    row = _opp(historical_awards=[])
    comp = build_competition_profile(row)
    assert comp["profile"] == PROFILE_UNKNOWN
    pkg = build_competitive_intelligence(row)
    assert pkg["FIRST_DEAL"]["priority"] in {PRIORITY_HIGH, "MEDIUM_PRIORITY", "LOW_PRIORITY"}


def test_category_and_financeability():
    assert category_key(_opp(title="31--BUSHING,SLEEVE", agency="DEFENSE LOGISTICS AGENCY")) == "DLA_PARTS"
    assert category_key(_opp()) == "IT_EQUIPMENT"
    hard = build_financeability_profile(
        _opp(title="Custom classified installation services", estimated_value=5_000_000, supplier_intelligence={})
    )
    assert hard["classification"] == "DIFFICULT"


def test_analyze_and_deal_room(tmp_path):
    store = M3PipelineStore(path=tmp_path / "pipe.json", durable=False)
    store._rows["c1"] = _opp(canonical_id="c1")
    store._rows["c2"] = _opp(
        canonical_id="c2",
        title="Window Washing Services",
        product_category="LIKELY_SERVICE",
        estimated_value=20000,
        supplier_intelligence={},
        commercial_intelligence={"COMMERCIAL_OPPORTUNITY_SCORE": "LOW"},
    )
    store.save()
    out = analyze_competitive_top_opportunities(store, limit=5)
    assert out["analyzed"] >= 1
    assert out["paid"] == 0
    assert out["DEVELOPMENT_NO_OUTREACH"] is True
    assert out["TOP_10_FIRST_DEAL"]
    sec = deal_room_competitive_section(store.get("c1") or _opp(canonical_id="c1"))
    assert sec["kind"] == "M3DealRoomCompetitiveIntelligence"
    assert "First_deal_score" in sec
    assert sec["Why"] is not None
