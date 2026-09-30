"""Phase I hunt — scoring, eligibility filter, owner packet (no live network)."""

from __future__ import annotations

from eligibility_gate import ELIGIBILITY_NOT_APPLICABLE, NOT_CURRENTLY_ELIGIBLE
from phase_i.hunt import build_owner_packet, early_hard_reject, eligibility_screen, merge_inventories
from phase_i.scoring import deal_hunt_score


def test_deal_hunt_score_prefers_nsn():
    low = deal_hunt_score({"title": "Miscellaneous supplies"})
    high = deal_hunt_score({"title": "PARTS KIT,SEAL — NSN: 5330-01-123-4567", "is_dla": True})
    assert high["deal_hunt_score"] > low["deal_hunt_score"]
    assert high["has_nsn"] is True


def test_early_hard_reject_construction_and_sources_sought():
    assert early_hard_reject({"title": "Design-Build Repair Storm Sewer"}) == "service_construction_or_market_research"
    assert early_hard_reject({"title": "Sources Sought: Widget"}) == "service_construction_or_market_research"
    assert early_hard_reject({"title": "Repair of B-2 Transponder Set NSN: 5895-01-383-4052"}) == (
        "service_construction_or_market_research"
    )
    assert early_hard_reject({"title": "PARTS KIT ENGINE"}) is None


def test_ancient_history_is_weak_not_moderate():
    from phase_h.deep_research import _history_class

    ancient = [{"award_amount": 100000, "days_ago": 4749, "recency_weight": 0.0}]
    assert _history_class(ancient) == "WEAK_HISTORY"
    recent = [{"award_amount": 100000, "days_ago": 200, "recency_weight": 0.5}]
    assert _history_class(recent) == "MODERATE_HISTORY"


def test_eligibility_blocks_boast_before_deep():
    row = {
        "title": "BOAST RFOP - Panel, Power Distribution - NSN: 6110-01-082-8958",
        "description": "Limited to active BOAST Basic Ordering Agreement holders.",
    }
    elig = eligibility_screen(row)
    assert elig["status"] == NOT_CURRENTLY_ELIGIBLE
    assert elig["deep_research_allowed"] is False


def test_open_solicitation_eligibility_passes():
    row = {"title": "PARTS KIT,SEAL REPLACE NSN: 5330-01-111-2222", "description": "Open solicitation."}
    elig = eligibility_screen(row)
    assert elig["status"] == ELIGIBILITY_NOT_APPLICABLE
    assert elig["deep_research_allowed"] is True


def test_merge_inventories_dedupes():
    a = [{"canonical_id": "abc", "title": "A"}]
    b = [{"canonical_id": "abc", "title": "A-dup"}, {"canonical_id": "def", "title": "B"}]
    merged = merge_inventories(a, b)
    assert len(merged) == 2


def test_owner_packet_includes_quote_target():
    packet = build_owner_packet(
        {
            "phase_h_operator_packet": {
                "opportunity": {"solicitation": "X", "product": "Widget"},
                "market": {"quote_required": True},
                "compliance": {},
                "funding": {"state": "UNKNOWN"},
                "economics": {},
            },
            "eligibility_gate": {"overall_status": ELIGIBILITY_NOT_APPLICABLE, "plain": {}},
            "phase_h_history": {"awards": []},
            "phase_h_history_class": "MODERATE_HISTORY",
            "phase_h_max_supplier_cost": {"maximum_allowable_supplier_cost": 84500.0},
            "phase_h_readiness": "READY_FOR_QUOTE_OUTREACH",
            "phase_h_next_action": "Request quotes",
            "phase_h_nsn": "1234-01-234-5678",
        }
    )
    assert packet["economics"]["max_supplier_cost"] == 84500.0
    assert "84,500" in packet["economics"]["quote_target"]
    assert packet["kind"] == "PhaseIOwnerPacket"
