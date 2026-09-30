"""BUILD 21 — Action Orchestration Layer tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from m3_action_orchestration_read import (
    AT_DECISION_REVIEW,
    AT_RESEARCH,
    AT_VALIDATION,
    BUILD_TAG,
    EX_AI_AGENT,
    EX_HUMAN,
    ST_BLOCKED,
    ST_COMPLETED,
    ST_READY,
    action_type_catalog,
    add_dependency,
    assign_executor,
    attach_action_orchestration_to_deal_room,
    build_action_orchestration_profile,
    capture_action_outcome,
    complete_action,
    create_action,
    enrich_command_center_actions,
    evaluate_action_readiness,
    link_action_evidence,
    list_workflow_templates,
    record_action_result,
    start_workflow,
    trigger_action_from_event,
)


@pytest.fixture(autouse=True)
def _isolate_action_orchestration_persistence(monkeypatch):
    """
    Phase F Step 0 — isolate action indexes from shared AppSetting/SQLite.
    Shared DB persistence caused nondeterministic test_dependency_blocking under full-suite load
    (silent save contention / lost updates). In-memory indexes keep production code paths
    but remove cross-test interference.
    """
    import m3_action_orchestration_read as mod

    store: dict[str, dict] = {}

    def _load(key: str):
        base = store.setdefault(str(key), {"by_key": {}, "entries": []})
        return deepcopy(base)

    def _save(key: str, payload: dict):
        store[str(key)] = deepcopy(dict(payload))
        store[str(key)].setdefault("by_key", {})
        store[str(key)].setdefault("entries", [])
        return True

    monkeypatch.setattr(mod, "_load_index", _load)
    monkeypatch.setattr(mod, "_save_index", _save)
    yield store


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-action-orchestration-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_action_creation_requires_context():
    with pytest.raises(ValueError, match="trigger"):
        create_action({"title": "X", "description": "why"}, persist=False)
    with pytest.raises(ValueError, match="description|why"):
        create_action({"title": "X", "trigger_source": "unknown_created"}, persist=False)

    action = create_action(
        {
            "action_type": AT_RESEARCH,
            "title": "Verify supplier authorization path",
            "description": "UNKNOWN: supplier authorization status",
            "trigger_source": "unknown_created",
            "related_opportunity": "sol:a1",
            "evidence_caused_by": "gap:auth",
        },
        persist=False,
    )
    assert action["kind"] == "M3Action"
    assert action["status"] in {"CREATED", "READY"}
    assert action["why_exists"]
    assert action["lineage"]["parent_event"] == "unknown_created"
    assert action["ai_decides"] is False
    assert action["numeric_score"] is None


def test_trigger_creation_examples():
    quote = trigger_action_from_event(
        {
            "event": "evidence_expired",
            "what": "supplier_quote",
            "opportunity_id": "sol:q1",
            "evidence": "quote expired 2026-09-01",
        },
        persist=False,
    )
    assert "updated supplier quote" in quote["title"].lower()
    assert quote["trigger_source"] == "evidence_expired"

    unk = trigger_action_from_event(
        {
            "event": "unknown_created",
            "unknown_label": "supplier authorization status",
            "opportunity_id": "sol:u1",
        },
        persist=False,
    )
    assert "supplier authorization" in unk["title"].lower()
    assert "UNKNOWN" in unk["description"]


def test_dependency_blocking():
    identity = create_action(
        {
            "title": "Confirm product identity",
            "description": "Product identity must be evidenced before pricing",
            "trigger_source": "unknown_created",
            "action_type": AT_VALIDATION,
        },
        persist=True,
    )
    price = create_action(
        {
            "title": "Price opportunity",
            "description": "Cannot price until product identity confirmed",
            "trigger_source": "decision_requires_review",
            "action_type": AT_RESEARCH,
            "dependencies": [identity["action_id"]],
        },
        persist=True,
    )
    assert price["status"] == ST_BLOCKED
    assert identity["action_id"] in price["blocked_by"]

    add_dependency(action_id=price["action_id"], prerequisite_action_id=identity["action_id"], persist=True)
    ready = evaluate_action_readiness(price["action_id"], persist=True)
    assert ready["ready"] is False
    assert ready["status"] == ST_BLOCKED

    complete_action(
        action_id=identity["action_id"],
        result="NSN documented",
        evidence="dla field nsn present",
        criteria_met=["validation_status_recorded", "evidence_linked", "unknowns_explicit"],
        persist=True,
    )
    ready2 = evaluate_action_readiness(price["action_id"], persist=True)
    assert ready2["status"] == ST_READY
    assert ready2["ready"] is True



def test_executor_assignment_ai_guardrails():
    research = create_action(
        {
            "title": "Draft research notes",
            "description": "AI may draft; may not modify verified intelligence",
            "trigger_source": "new_intelligence_discovered",
            "action_type": AT_RESEARCH,
        },
        persist=True,
    )
    assigned = assign_executor(
        action_id=research["action_id"],
        executor_type=EX_AI_AGENT,
        persist=True,
    )
    assert assigned["executor"]["may_modify_verified_intelligence"] is False

    review = create_action(
        {
            "title": "Decision review",
            "description": "Human must review — AI does not decide",
            "trigger_source": "decision_requires_review",
            "action_type": AT_DECISION_REVIEW,
        },
        persist=True,
    )
    with pytest.raises(ValueError, match="AI agent cannot"):
        assign_executor(action_id=review["action_id"], executor_type=EX_AI_AGENT, persist=False)

    human = assign_executor(action_id=review["action_id"], executor_type=EX_HUMAN, persist=True)
    assert human["executor"]["required_approval"] is True


def test_completion_validation_and_evidence_linking():
    action = create_action(
        {
            "title": "Research supplier",
            "description": "Complete when identity, sources, relationships, unknowns recorded",
            "trigger_source": "unknown_created",
            "action_type": AT_RESEARCH,
        },
        persist=True,
    )
    with pytest.raises(ValueError, match="evidence"):
        complete_action(action_id=action["action_id"], result="done", evidence="", persist=False)

    linked = link_action_evidence(
        action_id=action["action_id"],
        evidence_packets=["epkt:1"],
        retrieval_sessions=["rs:1"],
        master_records=["mr:supplier:1"],
        decisions=["dec:1"],
        analysis_records=["an:1"],
        persist=True,
    )
    assert "epkt:1" in linked["evidence_links"]["evidence_packets"]
    assert "mr:supplier:1" in linked["evidence_links"]["master_records"]

    record_action_result(
        action_id=action["action_id"],
        result="two distributors found; quote UNKNOWN",
        evidence="catalog notes",
        persist=True,
    )
    done = complete_action(
        action_id=action["action_id"],
        result="sources captured; unknowns recorded",
        evidence="packet epkt:1 — unknowns: authorization",
        criteria_met=[
            "sources_checked_documented",
            "findings_or_unknowns_recorded",
            "evidence_references_captured",
        ],
        persist=True,
    )
    assert done["action"]["status"] == ST_COMPLETED
    assert done["trace"]["evidence"]
    assert done["trace"]["action"]


def test_outcome_capture():
    action = create_action(
        {
            "title": "Request supplier quote",
            "description": "Expect response in 3 days",
            "trigger_source": "evidence_expired",
            "action_type": "COMMUNICATION",
        },
        persist=True,
    )
    complete_action(
        action_id=action["action_id"],
        result="quote received",
        evidence="email thread",
        criteria_met=["communication_attempt_documented", "response_or_waiting_status", "evidence_of_contact_or_unknown"],
        persist=True,
    )
    outcome = capture_action_outcome(
        {
            "action_id": action["action_id"],
            "expected_outcome": "supplier responds in 3 days",
            "actual_outcome": "14 days",
            "variance": "11 days slower",
            "lesson_created": "Supplier response timing varies by category",
            "related_future_applicability": ["quote_followups", "category_lead_time"],
            "evidence": "email timestamps",
        },
        persist=True,
    )
    assert outcome["kind"] == "M3ActionOutcomeMemory"
    assert "varies by category" in outcome["lesson_created"]
    assert outcome["fabricated_score"] is False


def test_workflow_templates():
    catalog = action_type_catalog()
    assert len(catalog["types"]) == 8
    assert catalog["ai_may_modify_verified_intelligence"] is False

    templates = list_workflow_templates()
    keys = {t["key"] for t in templates["templates"]}
    assert "PRODUCT_OPPORTUNITY_REVIEW" in keys
    assert "SUPPLIER_QUALIFICATION" in keys
    assert "CONTRACT_EXECUTION" in keys

    wf = start_workflow(
        template_key="SUPPLIER_QUALIFICATION",
        opportunity_id="sol:wf1",
        persist=True,
    )
    assert wf["kind"] == "M3ActionWorkflow"
    assert len(wf["action_ids"]) == 4


def test_command_center_and_deal_attach():
    morning = enrich_command_center_actions(
        {},
        [{"canonical_id": "sol:cc1", "title": "Pump"}],
        period="morning",
    )
    assert "blocked_actions" in morning
    assert "unresolved_unknowns" in morning
    assert "execution_actions" in morning
    assert "expired_evidence" in morning

    evening = enrich_command_center_actions({}, [{"canonical_id": "sol:cc1"}], period="evening")
    assert "completed_actions" in evening
    assert "lessons_captured" in evening

    profile = build_action_orchestration_profile({"canonical_id": "sol:p1", "title": "Widget"})
    assert profile["ai_decides"] is False
    assert profile["does_not_replace_retrieval"] is True
    assert "ACTION ORCHESTRATION" in profile["flow"]

    deal = attach_action_orchestration_to_deal_room(
        {"canonical_id": "sol:p1"},
        row={"canonical_id": "sol:p1"},
    )
    assert deal["action_orchestration"]["kind"] == "M3ActionOrchestrationProfile"


def test_regression_prior_layers_and_cost_governor():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_contract_lifecycle_read import BUILD_TAG as CTR
    from m3_execution_os_read import BUILD_TAG as EOS
    from m3_intelligence_retrieval_read import BUILD_TAG as RET
    from m3_master_record_read import BUILD_TAG as MASTER

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert CTR.startswith("20260919-m3-contract-lifecycle")
    assert EOS.startswith("20260919-m3-execution-os")
    assert MASTER.startswith("20260919-m3-master-record")
    assert RET.startswith("20260919-m3-intelligence-retrieval")
