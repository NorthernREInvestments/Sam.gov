"""BUILD 23 — M3 Human Operating System tests."""

from __future__ import annotations

import pytest

from m3_human_os_read import (
    AI_STAGES,
    BUILD_TAG,
    ROLE_BEGINNER,
    WORKFLOW_STEPS,
    attach_human_os_to_deal_room,
    beginner_mode_pack,
    build_decision_workspace,
    build_execution_workspace,
    build_home_today,
    build_human_os_profile,
    build_human_review,
    build_opportunity_workspace,
    build_research_workspace,
    build_supplier_workspace,
    complete_onboarding_step,
    enrich_command_center_human_os,
    humanize_task,
    infer_workflow_step,
    list_human_tasks,
    list_user_roles,
    onboarding_walkthrough,
    plain_label,
    plan_ai_user_action,
    record_ai_workflow_cost,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-human-operating-system-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_onboarding_workflow():
    walk = onboarding_walkthrough()
    assert walk["kind"] == "M3OnboardingWalkthrough"
    assert walk["goal_minutes"] == "15–30"
    assert len(walk["steps"]) == 5
    assert walk["not_a_database"] is True
    prog = complete_onboarding_step(1, user_id="test-user", persist=False)
    assert prog["completed_step"] == 1


def test_workspace_creation_and_navigation():
    row = {
        "canonical_id": "sol:hos1",
        "title": "Hydraulic pump",
        "buyer": "DLA",
        "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}}},
        "supplier_product_graph": {"edges": [{"supplier_name": "Acme Dist", "role": "distributor"}]},
    }
    opp = build_opportunity_workspace(row)
    assert opp["what_is_this"] == "Hydraulic pump"
    assert opp["who_needs_it"] == "DLA"
    assert opp["what_product"] == "4320-01-243-1951"
    assert "Acme Dist" in opp["who_can_supply"]
    assert opp["does_not_duplicate_data"] is True

    wf = infer_workflow_step(row)
    assert wf["current_step"] >= 3
    assert len(WORKFLOW_STEPS) == 7
    assert "Step" in wf["you_are_here"]

    sup = build_supplier_workspace({"supplier_name": "Acme Dist"}, row=row)
    assert sup["who_is_this_supplier"] == "Acme Dist"
    assert any("Confirm supplier" in a["what"] for a in sup["what_actions_needed"])

    research = build_research_workspace(
        {
            "question": "Who can supply NSN 4320?",
            "sources_checked": ["supplier catalog"],
            "evidence_found": ["Acme Dist listed"],
            "unknowns": ["quote"],
        }
    )
    assert research["research_question"].startswith("Who can")

    decision = build_decision_workspace({}, row=row)
    assert decision["humans_decide"] is True
    assert decision["ai_decides"] is False

    execution = build_execution_workspace({}, row=row)
    assert execution["current_commitments"]


def test_beginner_mode_plain_language():
    conflict = plain_label("Master Record Conflict")
    assert "two different pieces" in conflict["plain"].lower()
    gap = plain_label("Knowledge Gap")
    assert "missing information" in gap["plain"].lower()

    pack = beginner_mode_pack()
    assert pack["enabled_by_default_for"] == ROLE_BEGINNER
    assert pack["tooltips"]["Home"]
    assert len(pack["glossary"]) >= 5


def test_task_integration_with_action_orchestration():
    from m3_action_orchestration_read import create_action

    action = create_action(
        {
            "title": "Confirm supplier availability",
            "description": "This opportunity cannot move forward until we know whether the supplier can provide the product.",
            "trigger_source": "unknown_created",
            "related_opportunity": "sol:hos-task",
        },
        persist=True,
    )
    task = humanize_task(action)
    assert task["what_needs_done"] == "Confirm supplier availability"
    assert "cannot move forward" in task["why_it_matters"]
    assert task["from_action_orchestration"] is True

    listed = list_human_tasks(opportunity_id="sol:hos-task", limit=10)
    assert listed["does_not_duplicate_actions"] is True
    assert any(t["action_id"] == action["action_id"] for t in listed["tasks"])


def test_review_workflows_and_decision_trace():
    review = build_human_review(
        "Opportunity Review",
        payload={
            "evidence": ["NSN documented"],
            "known": ["buyer DLA"],
            "unknown": ["supplier quote"],
            "action": "Continue research",
            "outcome": "UNKNOWN",
        },
    )
    assert review["flow"] == ["Question", "Evidence", "Known", "Unknown", "Action", "Outcome"]
    assert review["humans_decide"] is True
    assert review["no_scores"] is True

    for rt in ("Supplier Review", "Product Review", "Economic Review", "Execution Review"):
        assert build_human_review(rt)["review_type"] == rt


def test_ai_escalation_points_and_cost_tracking():
    assert AI_STAGES[0]["uses_ai"] is False
    assert AI_STAGES[5]["uses_ai"] is True

    plan0 = plan_ai_user_action("find_next_steps", opportunity_id="sol:ai1")
    assert plan0["funnel_stage"] == 0
    assert plan0["executes_ai_call"] is False
    assert plan0["staged_ai_escalation_preserved"] if False else plan0["unnecessary_ai_disallowed"] is True

    plan = plan_ai_user_action("analyze_this", opportunity_id="sol:ai1")
    assert plan["funnel_stage"] == 1
    assert plan["model_routing"]["stage5_requires_explicit_user_action"] is True
    assert plan["caching"]["prefer_cache"] is True

    premium = plan_ai_user_action("analyze_this", force_stage=5, allow_stage5=False)
    assert premium["funnel_stage"] <= 2
    assert premium["model_routing"]["premium_blocked_without_allow"] is True

    allowed = plan_ai_user_action("analyze_this", force_stage=5, allow_stage5=True)
    assert allowed["funnel_stage"] == 5

    cost = record_ai_workflow_cost(
        {
            "action_key": "analyze_this",
            "opportunity_id": "sol:ai1",
            "funnel_stage": 1,
            "cache_hit": True,
            "estimated_cost_usd": 0.0,
            "escalation_reason": "cache reuse",
            "cost_state": "REUSED_EXISTING_EVIDENCE",
        },
        persist=False,
    )
    assert cost["cache_hit"] is True
    assert cost["kind"] == "M3HumanOsAiCost"

    with pytest.raises(ValueError, match="unknown AI action"):
        plan_ai_user_action("invent_strategy")


def test_home_command_center_and_roles():
    morning = build_home_today([{"canonical_id": "sol:h1", "title": "Pump"}], period="morning")
    assert morning["headline"] == "Today"
    assert morning["plain_language"] is True
    assert morning["hides_architecture"] is True
    assert "actions_requiring_attention" in morning

    evening = build_home_today([], period="evening")
    assert evening["period"] == "evening"
    assert "outstanding_issues" in evening

    roles = list_user_roles()
    assert len(roles["roles"]) == 5

    cc_m = enrich_command_center_human_os({}, [{"canonical_id": "sol:h1"}], period="morning")
    assert "what_needs_attention" in cc_m
    assert "what_is_blocked" in cc_m
    cc_e = enrich_command_center_human_os({}, [], period="evening")
    assert "completed_work" in cc_e or "outstanding_issues" in cc_e


def test_profile_deal_attach():
    profile = build_human_os_profile(
        {"canonical_id": "sol:p1", "title": "Widget"},
        role=ROLE_BEGINNER,
    )
    assert profile["not_a_database_replacement"] is True
    assert profile["staged_ai_escalation_preserved"] is True
    assert profile["ai_assists_humans_decide"] is True
    assert profile["no_scores"] is True
    assert profile["beginner_mode"]["kind"] == "M3BeginnerMode"

    deal = attach_human_os_to_deal_room({"canonical_id": "sol:p1"}, row={"canonical_id": "sol:p1", "title": "Widget"})
    assert deal["human_os"]["kind"] == "M3HumanOperatingSystemProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_action_orchestration_read import BUILD_TAG as ACT
    from m3_intelligence_retrieval_read import BUILD_TAG as RET
    from m3_master_record_read import BUILD_TAG as MASTER
    from m3_strategic_intelligence_read import BUILD_TAG as STRAT

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ACT.startswith("20260919-m3-action-orchestration")
    assert RET.startswith("20260919-m3-intelligence-retrieval")
    assert MASTER.startswith("20260919-m3-master-record")
    assert STRAT.startswith("20260919-m3-strategic-intelligence")
    assert len(AI_STAGES) == 6
