"""Permanent solicitation SOP — stage order, retention, rejection, paid-call gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from m3.research.deadline_retention import retention_state
from m3.research.rejection_registry import (
    record_rejection,
    should_block_reacquisition,
)
from m3.research.solicitation_sop import (
    SOP_STAGES,
    SopBlocked,
    assert_acquisition_decision_allows_spend,
    expected_call_scenarios,
    mark_stage,
    plan_acquisition_decision,
    require_prior_stages,
    sop_stage_function_map,
)


def test_sop_stages_ordered_owner_machine():
    assert SOP_STAGES[0] == "REGISTERED"
    assert SOP_STAGES[-1] == "COMPLETE"
    assert "AI_INITIAL_TRIAGE" in SOP_STAGES
    assert "AI_FULL_ANALYSIS" in SOP_STAGES
    assert len(SOP_STAGES) >= 16


def test_cannot_skip_stages():
    state = {"nb_id": "NB_TEST", "stages_completed": ["REGISTERED"]}
    with pytest.raises(SopBlocked):
        require_prior_stages(state, "AI_FULL_ANALYSIS")


def test_mark_stage_enforces_order():
    state = {"nb_id": "NB_TEST", "stages_completed": []}
    mark_stage(state, "REGISTERED")
    with pytest.raises(SopBlocked):
        mark_stage(state, "SOURCE_ASSEMBLED")


def test_paid_spend_blocked_without_decision():
    with pytest.raises(SopBlocked):
        assert_acquisition_decision_allows_spend(None)
    with pytest.raises(SopBlocked):
        assert_acquisition_decision_allows_spend({"approved": False, "spend": False})


def test_complete_local_scenario_zero_calls():
    package = {
        "coverage": {"has_substantive_document_content": True, "total_content_chars": 5000},
        "document_texts": [
            {
                "doc_id": "t",
                "kind": "full_text_store",
                "chars": 5000,
                "text": "x" * 5000,
                "is_metadata_only": False,
                "is_listing_preview": False,
            }
        ],
        "assembled_text": "quantity each specification deadline deliver submit " + ("word " * 200),
        "documents": [
            {"doc_id": "t", "chars": 5000, "has_text": True, "is_metadata_only": False, "is_listing_preview": False}
        ],
    }
    from m3.research.solicitation_sop import coverage_label_from_package

    # Text length alone must NOT yield complete_original_package
    label = coverage_label_from_package(package)
    assert label in {
        "substantive_incomplete_package",
        "substantive_solicitation_body",
        "package_completeness_unverified",
        "partial_document_package",
    }
    assert label != "complete_original_package"
    # With explicit complete_relevant package completeness, zero spend
    package["coverage"]["package_completeness"] = "complete_relevant_package"
    package["coverage"]["document_inventory"] = {"downloaded": 1, "download_pending": 0}
    assert coverage_label_from_package(package) == "complete_relevant_package"
    d = plan_acquisition_decision(nb_id="NB1", package=package, missing=[], allow_nextbid=False)
    assert d["expected_nextbid_calls"] == 0
    assert d["spend"] is False


def test_scenarios_and_stage_map_present():
    assert len(expected_call_scenarios()) == 7
    m = sop_stage_function_map()
    assert set(m.keys()) == set(SOP_STAGES)


def test_rejection_fingerprint_blocks_unchanged(tmp_path, monkeypatch):
    from m3 import config as cfg

    class S:
        def data_root(self):
            return tmp_path

    monkeypatch.setattr(cfg, "get_settings", lambda: S())
    row = record_rejection(
        nb_id="NB999",
        solicitation_number="SOL-1",
        category="construction",
        content_hash="abc123",
        reason="test_construction",
        evidence_passage="construction of building",
    )
    assert row["further_acquisition_prohibited"] is True
    blocked, prior = should_block_reacquisition(nb_id="NB999", content_hash="abc123")
    assert blocked is True
    assert prior and prior["nb_id"] == "NB999"
    # Material content change → reconsideration path (not auto-blocked)
    blocked2, prior2 = should_block_reacquisition(nb_id="NB999", content_hash="zzz_new")
    assert blocked2 is False
    assert prior2.get("reconsideration_candidate") is True


def test_retention_48h_and_stale_open_never_overrides_deadline():
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    item = {
        "nb_id": "NB1",
        "deadline_at": past,
        "response_deadline": past,
        "open": True,  # stale OPEN must not keep actionable
        "status": "OPEN",
    }
    st = retention_state(item)
    assert st["deadline_known"] is True
    assert st["expired"] is True
    assert st["actionable"] is False
    assert st["working_retain"] is True  # within 48h

    ancient = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    item2 = {"nb_id": "NB2", "deadline_at": ancient, "response_deadline": ancient}
    st2 = retention_state(item2)
    assert st2["past_48h_window"] is True
    assert st2["working_retain"] is False


def test_missing_deadline_not_auto_deleted():
    st = retention_state({"nb_id": "NB3", "open": True})
    assert st["deadline_known"] is False
    assert st["working_retain"] is True
    assert st["reason"].startswith("deadline_missing")
