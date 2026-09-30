"""BUILD 25 — Research Mission Execution Layer tests."""

from __future__ import annotations

import pytest

from m3_research_execution_read import (
    BUILD_TAG,
    ST_COMPLETE,
    ST_EVIDENCE_FOUND,
    ST_READY,
    attach_research_execution_to_deal_room,
    build_research_execution_profile,
    create_missions_from_supply_unknowns,
    create_research_mission,
    create_research_outcome,
    create_research_source,
    create_research_task,
    enrich_command_center_research_execution,
    get_research_mission,
    get_research_task,
    list_research_missions,
    research_workspace_for_mission,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-research-execution-1"
    assert APP_BUILD_VERSION.startswith("20260922-m3-")


def test_mission_creation_requires_context():
    with pytest.raises(ValueError, match="reason"):
        create_research_mission(
            {"question": "Find supplier", "required_evidence": ["catalog"]},
            persist=False,
        )
    with pytest.raises(ValueError, match="required_evidence"):
        create_research_mission(
            {"question": "Find supplier", "reason": "gap"},
            persist=False,
        )

    mission = create_research_mission(
        {
            "question": "Find suppliers for product",
            "reason": "Supplier missing from supply path",
            "required_evidence": ["Supplier relationship evidence"],
            "opportunity_id": "sol:rx1",
            "related_product": "Hydraulic pump",
            "created_from": "supply_unknown:Supplier not evidenced",
            "create_action": True,
            "tasks": [
                {
                    "question": "Find supplier",
                    "source_type": "Distributor",
                    "instructions": "Identify evidenced distributor",
                    "evidence_required": ["Supplier relationship evidence"],
                }
            ],
        },
        persist=True,
    )
    assert mission["kind"] == "M3ResearchMissionExec"
    assert mission["ai_autonomous"] is False
    assert mission["status"] in {ST_READY, "CREATED", "READY"}
    assert mission["task_ids"]
    assert mission["linked_action_ids"]
    assert get_research_mission(mission["mission_id"])["question"]


def test_task_creation_and_evidence_linkage():
    mission = create_research_mission(
        {
            "question": "Find manufacturer",
            "reason": "Manufacturer missing",
            "required_evidence": ["OEM documentation"],
            "opportunity_id": "sol:rx2",
            "create_action": False,
        },
        persist=True,
    )
    task = create_research_task(
        {
            "mission_id": mission["mission_id"],
            "question": "Find manufacturer",
            "source_type": "Manufacturer",
            "evidence_required": ["OEM documentation"],
            "owner": "human",
        },
        persist=True,
    )
    assert task["kind"] == "M3ResearchTaskExec"
    assert get_research_task(task["task_id"])["mission_id"] == mission["mission_id"]

    src = create_research_source(
        {
            "source": "manufacturer catalog PDF",
            "source_type": "Manufacturer",
            "related_mission": mission["mission_id"],
            "evidence_created": ["oem_doc:1"],
            "reliability_notes": "public catalog page",
        },
        persist=True,
    )
    assert src["evidence_created"] == ["oem_doc:1"]
    updated = get_research_mission(mission["mission_id"])
    assert updated["status"] == ST_EVIDENCE_FOUND


def test_outcome_and_unknown_resolution():
    mission = create_research_mission(
        {
            "question": "Confirm product identity",
            "reason": "Product identity not evidenced",
            "required_evidence": ["NSN/part/document"],
            "opportunity_id": "sol:rx3",
            "create_action": False,
        },
        persist=True,
    )
    outcome = create_research_outcome(
        {
            "mission_id": mission["mission_id"],
            "knowns": ["NSN 4320-01-243-1951 documented"],
            "unknowns_remaining": [],
            "evidence_added": ["dla_field:nsn"],
            "records_updated": ["supply_product"],
            "lessons": "Always capture NSN before supplier research",
        },
        persist=True,
    )
    assert outcome["ai_decided"] is False
    done = get_research_mission(mission["mission_id"])
    assert done["status"] == ST_COMPLETE
    assert "NSN" in " ".join(done.get("unknowns_resolved") or [])


def test_supply_intelligence_integration():
    missions = create_missions_from_supply_unknowns(
        opportunity_id="sol:rx-sup",
        unknowns=["Supplier not evidenced", "Need supplier pricing"],
        product="Pump",
        supplier="UNKNOWN",
        persist=True,
    )
    assert len(missions) >= 2
    questions = " ".join(m["question"].lower() for m in missions)
    assert "supplier" in questions or "pricing" in questions or "quote" in questions


def test_action_integration():
    from m3_action_orchestration_read import get_action

    mission = create_research_mission(
        {
            "question": "Obtain supplier quote",
            "reason": "Commercial evidence missing",
            "required_evidence": ["Quote"],
            "opportunity_id": "sol:rx-act",
            "create_action": True,
        },
        persist=True,
    )
    assert mission["linked_action_ids"]
    action = get_action(mission["linked_action_ids"][0])
    assert action is not None
    assert "quote" in (action.get("title") or "").lower() or "Obtain" in (action.get("title") or "")


def test_human_os_display():
    from m3_human_os_read import build_research_workspace

    mission = create_research_mission(
        {
            "question": "Validate acquisition path",
            "reason": "Path incomplete",
            "required_evidence": ["channel evidence"],
            "opportunity_id": "sol:rx-hos",
            "create_action": False,
            "tasks": [
                {
                    "question": "Document channel",
                    "evidence_required": ["channel evidence"],
                    "source_type": "Distributor",
                }
            ],
        },
        persist=True,
    )
    ws = research_workspace_for_mission(mission["mission_id"])
    assert ws["question"] == "Validate acquisition path"
    assert ws["why_it_matters"]
    assert ws["evidence_needed"]
    assert ws["tasks"]

    hos = build_research_workspace({"mission_id": mission["mission_id"], "opportunity_id": "sol:rx-hos"})
    assert hos["research_question"]
    assert hos["why_it_matters"]
    assert hos["research_execution"] is not None


def test_command_center_and_profile():
    create_research_mission(
        {
            "question": "Waiting mission",
            "reason": "gap",
            "required_evidence": ["doc"],
            "opportunity_id": "sol:rx-cc",
            "status": ST_READY,
            "create_action": False,
        },
        persist=True,
    )
    morning = enrich_command_center_research_execution(
        {},
        [{"canonical_id": "sol:rx-cc", "title": "Pump"}],
        period="morning",
    )
    assert "research_missions_waiting" in morning
    assert "research_evidence_needed" in morning

    evening = enrich_command_center_research_execution({}, [], period="evening")
    assert "research_missions_completed" in evening or "research_knowledge_added" in evening

    profile = build_research_execution_profile({"canonical_id": "sol:rx-cc", "title": "Pump"})
    assert profile["ai_controls"]["autonomous_agent"] is False
    assert profile["not_a_second_source_of_truth"] is True
    assert list_research_missions(opportunity_id="sol:rx-cc")["missions"]

    deal = attach_research_execution_to_deal_room(
        {"canonical_id": "sol:rx-cc"},
        row={"canonical_id": "sol:rx-cc"},
    )
    assert deal["research_execution"]["kind"] == "M3ResearchExecutionProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_human_os_read import BUILD_TAG as HOS
    from m3_supply_intelligence_read import BUILD_TAG as SUP

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert HOS.startswith("20260919-m3-human-operating-system")
    assert SUP.startswith("20260919-m3-supply-intelligence")
