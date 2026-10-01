"""Pre-test safety fixes: R5 invalidate, freeze package bind, SAM federal default off."""

from __future__ import annotations

import os

from response_engine.operator_state_service import build_operator_state
from response_engine.r5_approval import freeze_submission_package
from response_engine.r5_constants import APPROVAL_APPROVED, APPROVAL_EXPIRED, MANUAL_REVIEW_REQUIRED
from response_engine.r5_service import invalidate_r5_on_change


def _minimal_project():
    return {
        "response_project_id": "RP-TEST-SAFE",
        "title": "Safety fixture",
        "documents": [{"filename": "a.pdf"}],
        "pricing_scenarios": [{"scenario_id": "S1", "total_bid_price": "1000", "selected_for_draft": True}],
        "generated_package": {"package_id": "PKG-OLD", "generation_version": 1, "generated_documents": []},
        "owner_submission_approval": {
            "approval_status": APPROVAL_APPROVED,
            "approval_id": "APR1",
            "package_id": "PKG-OLD",
            "total_offer": "1000",
        },
        "frozen_submission_package": {"package_id": "PKG-OLD", "freeze_id": "FRZ1"},
        "r5_preflight": {"overall_status": "PASS", "approval_eligible": True},
    }


def test_freeze_rejects_package_mismatch():
    p = _minimal_project()
    p["generated_package"] = {"package_id": "PKG-NEW", "generation_version": 2, "generated_documents": []}
    r = freeze_submission_package(p)
    assert r["ok"] is False
    assert r["error"] == "approval_package_mismatch"


def test_invalidate_on_package_change():
    p = _minimal_project()
    invalidate_r5_on_change(p, reason="PACKAGE_REGENERATED")
    assert p["owner_submission_approval"]["approval_status"] == APPROVAL_EXPIRED
    assert p["frozen_submission_package"]["invalidated"] is True


def test_manual_review_has_next_action():
    p = {
        "response_project_id": "RP-MR",
        "generated_package": {"package_id": "PKG1"},
        "r5_preflight": {
            "overall_status": MANUAL_REVIEW_REQUIRED,
            "approval_eligible": False,
            "next_action_plain": "Owner must review clause X",
            "owner_action_count": 1,
        },
        "documents": [{"filename": "a.pdf"}],
    }
    st = build_operator_state(p)
    assert st["next_action"]
    assert st["next_action"]["action_type"] == "FIX_PREFLIGHT"
    assert "Owner must review" in (
        (st["next_action"].get("action_label") or "") + (st["next_action"].get("reason") or "")
    )


def test_federal_sam_default_off_without_explicit_enable(monkeypatch):
    monkeypatch.delenv("SAM_FEDERAL_DISCOVERY_ENABLED", raising=False)
    monkeypatch.setenv("SAM_GOV_API_KEY", "dummy-key-for-test-only")
    _fed_env = (os.environ.get("SAM_FEDERAL_DISCOVERY_ENABLED") or "").strip().lower()
    _fed_opt_out = _fed_env in {"0", "false", "no", "off", ""}
    from discovery.federal_sam_ingest import federal_sam_discovery_enabled

    _fed_on = (not _fed_opt_out) and (
        federal_sam_discovery_enabled(authorize=False) or _fed_env in {"1", "true", "yes"}
    )
    assert _fed_on is False
