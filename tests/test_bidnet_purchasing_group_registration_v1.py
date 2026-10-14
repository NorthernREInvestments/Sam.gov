"""Regression tests for BidNet purchasing-group registration (offline)."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bidnet"


def test_extract_pg_gate_empire_state_fixture():
    from bidnet_auth.purchasing_group_registration import extract_pg_gate_from_html

    html = (FIXTURES / "private_abstract_pg_gate_empire_state_v1.html").read_text(encoding="utf-8")
    gate = extract_pg_gate_from_html(html)
    assert gate is not None
    assert gate["purchasing_group_id"] == "8411151"
    assert "Empire State" in (gate.get("name") or "")
    assert gate.get("complete_form_url")
    assert "group-form/edit" in gate["complete_form_url"]
    assert gate.get("fee_mentioned") is False
    assert gate.get("gate_kind") == "PURCHASING_GROUP_REGISTRATION_INCOMPLETE"


def test_prepare_empire_state_answers_no_invented_mbe():
    from bidnet_auth.purchasing_group_registration import prepare_empire_state_answers

    plan = prepare_empire_state_answers(
        {
            "legal_name": "Test Co",
            "ein_present": False,
            "ein_value": None,
            "certified_mbe": "NO",
            "other_mbe_dbe": "NO",
            "held_certifications": ["SB"],
            "mbe_verified_held": False,
            "dbe_verified_held": False,
        }
    )
    assert plan["owner_required"] is True
    assert plan["can_submit"] is False
    assert any(f["name"] == "fields[federalTaxPayerId]" for f in plan["missing_fields"])
    mbe = next(f for f in plan["fills"] if "certifiedMBE" in f["name"])
    assert mbe["value"] == "NO"


def test_prepare_empire_state_ready_when_ein_present():
    from bidnet_auth.purchasing_group_registration import prepare_empire_state_answers

    plan = prepare_empire_state_answers(
        {
            "legal_name": "Test Co",
            "ein_present": True,
            "ein_value": "12-3456789",
            "certified_mbe": "NO",
            "other_mbe_dbe": "NO",
            "held_certifications": ["SB"],
            "mbe_verified_held": False,
            "dbe_verified_held": False,
        }
    )
    assert plan["owner_required"] is False
    assert plan["can_submit"] is True
    assert not plan["missing_fields"]


def test_tracker_upsert_and_ui_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from bidnet_auth.purchasing_group_tracker import (
        STATUS_OWNER_APPROVAL_REQUIRED,
        STATUS_REGISTERED,
        is_registered,
        load_tracker,
        ui_summary,
        upsert_group,
    )

    upsert_group(
        "8411151",
        name="Empire State Purchasing Group",
        status=STATUS_OWNER_APPROVAL_REQUIRED,
        relevant=True,
        live_eligible_opportunity_count=12,
        owner_blocker_reason="Missing EIN",
        missing_fields=[{"name": "fields[federalTaxPayerId]"}],
    )
    t = load_tracker()
    assert "8411151" in t["groups"]
    assert t["metrics"]["owner_approval_required"] == 1
    assert len(t["pending_owner_actions"]) == 1
    assert not is_registered("8411151")

    upsert_group("8411151", status=STATUS_REGISTERED, registration_completed_at="2026-10-13T00:00:00Z")
    assert is_registered("8411151")
    ui = ui_summary()
    assert ui["metrics"]["registered"] == 1
    assert ui["groups"][0]["purchasing_group_id"] == "8411151"


def test_classify_blocker_pg_registration():
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import (
        PG_REGISTRATION_REQUIRED,
        classify_document_blocker,
    )

    blocker, ev = classify_document_blocker(
        auth_ok=True,
        auth_status="AUTHENTICATED",
        paid_requirement=None,
        paid_intercept=None,
        package_state="PACKAGE_PG_REGISTRATION_OWNER_REQUIRED",
        detail_status="DETAIL_ACCESS_GATE",
        docs=[],
        free_chase_status=None,
        fetch_error=None,
        timed_out=False,
    )
    assert blocker == PG_REGISTRATION_REQUIRED
    assert ev["classification_basis"] == "purchasing_group_registration_gate"


def test_is_registered_skips_duplicate():
    """Registration engine short-circuits when tracker already REGISTERED."""
    import bidnet_auth.purchasing_group_registration as mod

    class _C:
        pass

    # Monkeypatch is_registered via tracker state is covered in tracker test;
    # here verify run_empire_state early return shape when registered.
    from unittest.mock import patch

    with patch.object(mod, "is_registered", return_value=True):
        out = mod.run_empire_state_registration(_C())
    assert out["skipped"] is True
    assert out["status"] == "REGISTERED"
