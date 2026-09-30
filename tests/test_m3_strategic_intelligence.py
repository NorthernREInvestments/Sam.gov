"""BUILD 22 — Strategic Intelligence Layer tests."""

from __future__ import annotations

import pytest

from m3_strategic_intelligence_read import (
    BUILD_TAG,
    ST_OBSERVED,
    attach_strategic_intelligence_to_deal_room,
    build_opportunity_ecosystem_view,
    build_strategic_intelligence_profile,
    create_capability,
    create_capability_gap,
    create_market_observation,
    create_strategic_decision_trace,
    create_strategic_intelligence,
    create_strategic_memory,
    create_strategic_relationship,
    create_strategic_research_mission,
    create_trend_observation,
    enrich_command_center_strategic,
    generate_strategic_brief,
    get_strategic_brief,
    get_strategic_intelligence,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-strategic-intelligence-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_strategic_record_creation_and_evidence():
    with pytest.raises(ValueError, match="evidence"):
        create_strategic_intelligence({"strategic_topic": "Pumps"}, persist=False)

    rec = create_strategic_intelligence(
        {
            "strategic_topic": "Hydraulic pump supplier concentration",
            "supporting_evidence": ["award history notes", "supplier graph edges"],
            "related_markets": ["NSN 4320"],
            "related_products": ["hydraulic pump"],
            "unknowns": ["manufacturer exclusivity"],
            "research_opportunities": ["Map distributor channels"],
            "status": ST_OBSERVED,
        },
        persist=True,
    )
    assert rec["kind"] == "M3StrategicIntelligence"
    assert rec["numeric_score"] is None
    assert rec["ranking"] is None
    assert rec["prediction"] is None
    assert get_strategic_intelligence(rec["strategic_id"])["strategic_topic"]


def test_market_observation_rejects_forecast_language():
    with pytest.raises(ValueError, match="forecast|expanding|decline"):
        create_market_observation(
            {
                "category": "pumps",
                "observation": "The market is expanding",
                "evidence": ["3 new listings"],
            },
            persist=False,
        )

    mkt = create_market_observation(
        {
            "category": "pumps",
            "observation": "Two suppliers added related product lines",
            "evidence": ["catalog updates 2026-09"],
            "affected_products": ["NSN 4320-01"],
            "affected_suppliers": ["Acme Dist"],
            "unknowns": ["whether listings are new SKUs or rebrands"],
        },
        persist=True,
    )
    assert mkt["no_growth_inference"] is True
    assert mkt["kind"] == "M3MarketEvolution"


def test_capability_and_gaps():
    with pytest.raises(ValueError, match="evidence"):
        create_capability({"capability": "Product sourcing"}, persist=False)

    cap = create_capability(
        {
            "capability": "Product sourcing",
            "evidence": ["Completed supplier research", "Documented sourcing paths"],
            "originating_activities": ["action:research-1"],
            "limitations": ["No direct manufacturer agreements evidenced"],
        },
        persist=True,
    )
    assert cap["declared_without_evidence"] is False

    gap = create_capability_gap(
        {
            "missing_capability": "Limited manufacturer relationships",
            "why_it_matters": "Blocks verification of authorization paths",
            "possible_actions": ["Research manufacturer channels"],
            "required_research": ["OEM channel maps"],
            "evidence": ["ecosystem view shows UNKNOWN manufacturers"],
        },
        persist=True,
    )
    assert gap["kind"] == "M3CapabilityGap"
    assert "Research manufacturer" in gap["possible_actions"][0]


def test_relationship_mapping_no_assumptions():
    with pytest.raises(ValueError, match="evidence"):
        create_strategic_relationship(
            {"entity_a": "OEM", "entity_b": "Dist", "relationship_type": "distributes"},
            persist=False,
        )

    rel = create_strategic_relationship(
        {
            "entity_a": "Acme Mfg",
            "entity_b": "Beta Dist",
            "relationship_type": "Manufacturer → Distributor",
            "evidence": ["distributor catalog lists Acme SKU"],
            "observations": ["listed on public catalog page"],
        },
        persist=True,
    )
    assert rel["no_assumptions"] is True


def test_ecosystem_view_preserves_unknown():
    eco = build_opportunity_ecosystem_view(
        {
            "canonical_id": "sol:eco1",
            "title": "Pump",
            "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
            "supplier_product_graph": {
                "edges": [{"supplier_name": "Acme", "role": "distributor"}]
            },
            "buyer": "DLA",
        }
    )
    assert eco["connected_not_isolated"] is True
    assert eco["product_family"][0]["id"] == "4320-01-243-1951"
    assert eco["suppliers"][0]["name"] == "Acme"
    assert any(a["name"] == "DLA" for a in eco["agencies"])

    bare = build_opportunity_ecosystem_view({"canonical_id": "sol:bare"})
    assert bare["suppliers"][0]["name"] == "UNKNOWN"
    assert bare["contracts"][0]["id"] == "UNKNOWN"


def test_strategic_memory_missions_briefs_traces():
    mem = create_strategic_memory(
        {
            "strategic_question": "Should this category receive additional research?",
            "evidence_reviewed": ["3 related awards", "2 supplier listings"],
            "known_information": ["NSN family present"],
            "unknown_information": ["authorization path"],
            "actions_taken": ["opened research mission"],
            "outcome": "research prioritized",
            "lessons": "Category needs manufacturer channel map before pricing",
            "future_applicability": ["similar NSN families"],
            "related_opportunities": ["sol:eco1"],
        },
        persist=True,
    )
    assert mem["kind"] == "M3StrategicMemory"

    mission = create_strategic_research_mission(
        {
            "objective": "Understand supplier ecosystem",
            "trigger": "Multiple agencies buying related products",
            "required_evidence": ["distributor lists", "OEM channel notes"],
            "related_intelligence": [mem["memory_id"]],
        },
        persist=True,
    )
    assert mission["automated_decision"] is False
    assert mission["results"] == "UNKNOWN"

    brief = generate_strategic_brief(
        {
            "brief_type": "supplier",
            "subject": "Pump distributors",
            "known_facts": ["Acme listed as distributor"],
            "evidence": ["supplier graph"],
            "unknowns": ["lead times"],
            "research_opportunities": ["Request quote"],
        },
        persist=True,
    )
    assert brief["brief_type"] == "Supplier Ecosystem Brief"
    assert brief["no_predictions"] is True
    assert get_strategic_brief(brief["brief_id"])["brief_id"] == brief["brief_id"]

    trace = create_strategic_decision_trace(
        {
            "question": "Pursue additional category research?",
            "evidence": ["awards", "listings"],
            "known": ["product family"],
            "unknown": ["OEM path"],
            "action": "Start research mission",
            "outcome": "mission opened",
        },
        persist=True,
    )
    assert trace["flow"][0] == "Question"
    assert trace["human_judgment_required"] is True
    assert trace["automated_decision"] is False


def test_trend_observation_guard():
    with pytest.raises(ValueError, match="expanding"):
        create_trend_observation(
            {"observation": "The market is expanding", "evidence": ["x"]},
            persist=False,
        )
    obs = create_trend_observation(
        {
            "observation": "Multiple suppliers added product line",
            "evidence": ["catalog diffs"],
            "timeframe": "2026-Q3",
            "related_entities": ["Acme", "Beta"],
        },
        persist=True,
    )
    assert obs["not_a_forecast"] is True


def test_command_center_and_deal_attach():
    morning = enrich_command_center_strategic(
        {},
        [{"canonical_id": "sol:cc1", "title": "Pump"}],
        period="morning",
    )
    assert "strategic_research_missions" in morning
    assert "capability_gaps" in morning or "new_observations" in morning

    evening = enrich_command_center_strategic({}, [{"canonical_id": "sol:cc1"}], period="evening")
    assert "new_strategic_knowledge" in evening or "lessons_created" in evening

    profile = build_strategic_intelligence_profile({"canonical_id": "sol:p1", "title": "Widget"})
    assert profile["not_a_prediction_engine"] is True
    assert profile["no_scores"] is True
    flow = profile["architecture_flow"]
    assert "STRATEGIC INTELLIGENCE" in flow
    assert flow.index("EXECUTION INTELLIGENCE") < flow.index("STRATEGIC INTELLIGENCE")
    assert flow.index("STRATEGIC INTELLIGENCE") < flow.index("DECISION SUPPORT")
    assert flow.index("DECISION SUPPORT") < flow.index("ACTION ORCHESTRATION")
    assert flow[-1] == "LEARNING MEMORY"

    deal = attach_strategic_intelligence_to_deal_room(
        {"canonical_id": "sol:p1"},
        row={"canonical_id": "sol:p1"},
    )
    assert deal["strategic_intelligence"]["kind"] == "M3StrategicIntelligenceProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_intelligence_retrieval_read import BUILD_TAG as RET
    from m3_master_record_read import BUILD_TAG as MASTER

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert RET.startswith("20260919-m3-intelligence-retrieval")
    assert MASTER.startswith("20260919-m3-master-record")
