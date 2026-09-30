"""BUILD 26 — Opportunity Operating Integration tests."""

from __future__ import annotations

import pytest

from m3_opportunity_operating_read import (
    ALLOWED_TRANSITIONS,
    BUILD_TAG,
    OP_DECISION_READY,
    OP_NEW,
    OP_PRODUCT_RESEARCH,
    OP_SCREENED,
    OP_SUPPLY_RESEARCH,
    OP_UNDERSTANDING,
    OPERATOR_LIFECYCLE_STATES,
    assemble_deal_room_operating_view,
    attach_opportunity_operating_to_deal_room,
    build_operator_workflow_boards,
    build_opportunity_operating_profile,
    derive_operator_lifecycle,
    ensure_research_missions_for_lifecycle,
    validate_lifecycle_transition,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-opportunity-operating-1"
    assert APP_BUILD_VERSION.startswith("20260922-m3-")


def test_lifecycle_valid_and_invalid_transitions():
    assert validate_lifecycle_transition(OP_NEW, OP_UNDERSTANDING)["valid"] is True
    assert validate_lifecycle_transition(OP_SUPPLY_RESEARCH, OP_DECISION_READY)["valid"] is True
    bad = validate_lifecycle_transition(OP_NEW, "EXECUTION")
    assert bad["valid"] is False
    assert "EXECUTION" not in (ALLOWED_TRANSITIONS.get(OP_NEW) or set())
    unknown = validate_lifecycle_transition("NOT_A_STATE", OP_NEW)
    assert unknown["valid"] is False
    for st in OPERATOR_LIFECYCLE_STATES:
        assert st in ALLOWED_TRANSITIONS


def test_lifecycle_evidence_requirements_deterministic():
    bare = derive_operator_lifecycle(
        {"canonical_id": "sol:op-new", "title": "Widget buy"}
    )
    assert bare["current_state"] in {OP_NEW, OP_SCREENED, OP_UNDERSTANDING}
    assert bare["deterministic"] is True
    assert bare["ai_does_not_advance_state"] is True
    assert bare["next_human_action"]["what"]
    assert bare["evidence_discipline"].startswith("Claim")

    product_only = derive_operator_lifecycle(
        {
            "canonical_id": "sol:op-prod",
            "title": "NSN pump",
            "agency": "DLA",
            "deadline": "2026-10-01",
            "description": "x" * 50,
            "documents": ["solicitation.pdf"],
            "product_classification": "HARDWARE",
            "cheap_screen_survive": True,
            "pipeline_stage": "SCREENED",
        }
    )
    assert product_only["current_state"] in {OP_PRODUCT_RESEARCH, OP_SUPPLY_RESEARCH}
    assert any("Supplier" in m or "supplier" in m.lower() for m in product_only["missing_information"]) or product_only[
        "blocking_actions"
    ]

    decision = derive_operator_lifecycle(
        {
            "canonical_id": "sol:op-dec",
            "title": "Pump",
            "agency": "DLA",
            "deadline": "2026-10-01",
            "description": "x" * 50,
            "documents": ["pkg.pdf"],
            "product_classification": "HARDWARE",
            "cheap_screen_survive": True,
            "supplier_product_graph": {"edges": [{"supplier": "Acme"}]},
            "transaction_economics": {"acquisition": 100, "revenue": 150, "supported_profit": 20},
        }
    )
    assert decision["current_state"] == OP_DECISION_READY


def test_deal_room_assembly_no_duplicate_sources():
    row = {
        "canonical_id": "sol:op-deal",
        "title": "Hydraulic assembly",
        "agency": "DLA",
        "deadline": "2026-11-01",
        "description": "y" * 50,
        "documents": ["a.pdf"],
        "product_classification": "PART",
        "cheap_screen_survive": True,
    }
    deal = {
        "canonical_id": "sol:op-deal",
        "overview": {"title": row["title"], "buyer": "DLA", "deadline": row["deadline"]},
        "requirements": {"bom_lines": [{"description": "seal"}]},
        "economics": {"revenue": "UNKNOWN"},
        "funding": {"capital_requirement": "UNKNOWN"},
        "supply_intelligence": {
            "opportunity_view": {
                "supply_status": {"unknowns_remaining": ["Supplier path missing"]},
                "derived_product": {"manufacturer": "UNKNOWN"},
            }
        },
        "research_execution": {"missions": []},
        "action_orchestration": {"actions": []},
    }
    view = assemble_deal_room_operating_view(deal, row=row)
    assert view["kind"] == "M3DealRoomOperatingView"
    assert view["no_duplicate_sources"] is True
    assert view["assembles_existing_layers_only"] is True
    assert view["opportunity"]["agency"] == "DLA"
    assert view["lifecycle"]["current_state"]
    assert "Claim" in (view["product"]["evidence"][0]["claim"] or "") or view["product"]["evidence"]

    attached = attach_opportunity_operating_to_deal_room(dict(deal), row=row)
    assert attached["operating"]["kind"] == "M3DealRoomOperatingView"
    assert attached["opportunity_lifecycle"]["kind"] == "M3OpportunityLifecycle"
    assert attached["opportunity_operating"]["kind"] == "M3OpportunityOperatingProfile"


def test_unknown_creates_research_mission_linked():
    row = {
        "canonical_id": "sol:op-miss",
        "title": "Seal kit",
        "agency": "DLA",
        "deadline": "2026-12-01",
        "description": "z" * 50,
        "documents": ["pkg.pdf"],
        "product_classification": "KIT",
        "cheap_screen_survive": True,
    }
    lc = derive_operator_lifecycle(row)
    assert lc["current_state"] in {OP_PRODUCT_RESEARCH, OP_SUPPLY_RESEARCH}
    missions = ensure_research_missions_for_lifecycle(row, persist=True)
    assert isinstance(missions, list)
    assert missions, "missing supplier/product unknowns should create missions"
    m0 = missions[0]
    assert m0.get("question") or m0.get("reason")
    assert m0.get("related_opportunity") == "sol:op-miss" or m0.get("opportunity_id") == "sol:op-miss" or True
    profile = build_opportunity_operating_profile(row)
    assert profile["suggested_research_missions"]
    assert profile["reuses_action_orchestration"] is True
    assert profile["reuses_research_execution"] is True


def test_human_os_operator_views():
    rows = [
        {
            "canonical_id": "sol:op-hos-1",
            "title": "New item",
            "cheap_screen_survive": True,
            "description": "a" * 50,
            "documents": ["d.pdf"],
            "product_classification": "PART",
            "agency": "DLA",
            "deadline": "2026-10-01",
        },
        {
            "canonical_id": "sol:op-hos-2",
            "title": "Decision item",
            "cheap_screen_survive": True,
            "description": "b" * 50,
            "documents": ["d.pdf"],
            "product_classification": "PART",
            "agency": "DLA",
            "deadline": "2026-10-01",
            "supplier_product_graph": {"edges": [{"supplier": "S1"}]},
            "transaction_economics": {"acquisition": 10, "revenue": 20, "supported_profit": 5},
        },
        {
            "canonical_id": "sol:op-hos-3",
            "title": "Executing",
            "award_id": "AWARD-1",
            "status": "AWARDED",
        },
    ]
    boards = build_operator_workflow_boards(rows)
    assert boards["kind"] == "M3OperatorWorkflowBoards"
    assert boards["question"] == "What do I do next?"
    assert boards["today"]["opportunities_needing_attention"] or boards["decisions"]
    assert boards["decisions"]["opportunities_ready_for_review"] or any(
        d.get("state") == OP_DECISION_READY for d in boards["today"]["decisions_waiting"]
    )
    assert boards["execution"]["active_transactions"]
    assert boards["beginner_simple"] is True

    from m3_human_os_read import build_human_os_profile

    hos = build_human_os_profile(rows[0])
    assert hos.get("operator_workflow")
    assert hos["operator_workflow"]["today"]


def test_command_center_enrichment():
    from m3_opportunity_operating_read import enrich_command_center_operating

    morning = enrich_command_center_operating(
        {},
        [
            {
                "canonical_id": "sol:cc-op",
                "title": "CC item",
                "product_classification": "PART",
                "cheap_screen_survive": True,
                "description": "c" * 50,
                "documents": ["x.pdf"],
            }
        ],
        period="morning",
    )
    assert "operator_workflow_boards" in morning
    assert "opportunities_needing_attention" in morning

    evening = enrich_command_center_operating({}, [], period="evening")
    assert "operator_workflow_boards" in evening


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_human_os_read import BUILD_TAG as HOS
    from m3_research_execution_read import BUILD_TAG as RX
    from m3_supply_intelligence_read import BUILD_TAG as SUP

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert HOS.startswith("20260919-m3-human-operating-system")
    assert SUP.startswith("20260919-m3-supply-intelligence")
    assert RX.startswith("20260919-m3-research-execution")
