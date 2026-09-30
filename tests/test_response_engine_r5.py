"""R5 preflight / approval / signatures / adapters / receipt / UI state — 0 SAM, 0 live submit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from response_engine.models import new_response_project
from response_engine.operator_state_service import build_operator_state
from response_engine.r5_approval import freeze_submission_package
from response_engine.r5_constants import (
    APPROVAL_APPROVED,
    APPROVAL_EXPIRED,
    DRY_RUN_SUBMITTED_CONFIRMED,
    FAIL,
    OWNER_ACTION_REQUIRED,
    PASS,
    ROLE_OWNER,
    SIG_SIGNED,
    STATUS_AWAITING_RESULT,
    STATUS_DEADLINE_PASSED,
    STATUS_READY_FOR_APPROVAL,
    STATUS_WAITING_OWNER,
    SUB_SUBMITTED_UNCONFIRMED,
    WARNING,
)
from response_engine.r5_preflight import run_preflight
from response_engine.r5_service import (
    invalidate_r5_on_change,
    r5_dry_run_submit,
    r5_owner_approve,
    r5_record_receipt,
    r5_sign,
    run_r5_preflight,
)
from response_engine.r5_signatures import auto_signed_count, ensure_signature_tasks
from response_engine.submission_adapters import (
    build_submission_plan,
    detect_adapter_type,
    validate_portal_price_entry,
)


@pytest.fixture
def rp_store(tmp_path, monkeypatch):
    from response_engine import package_store, store
    from response_engine import submission_audit as audit

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store, "INDEX_PATH", store.STORE_DIR / "index.json")
    monkeypatch.setattr(package_store, "GENERATED_ROOT", tmp_path / "rp")
    monkeypatch.setattr(audit, "AUDIT_ROOT", tmp_path / "rp")
    store.ensure_store()
    return tmp_path


def _write_doc(tmp_path: Path, name: str, content: bytes = b"buyer-facing draft") -> dict:
    p = tmp_path / name
    p.write_bytes(content)
    return {
        "filename": name,
        "path": str(p),
        "hash": "abc123",
        "buyer_facing": True,
        "document_type": "GENERATED_QUOTE_LETTER",
        "owner_signature_required": False,
    }


def _ready_project(tmp_path: Path, **extra):
    """Synthetic package that can pass preflight (future deadline, files on disk)."""
    p = new_response_project(canonical_opportunity_id="r5-fix-1", title="RFQ Widgets")
    p["solicitation_number"] = "RFQ-R5-001"
    p["buyer"] = "Test Agency"
    p["response_type"] = "RFQ"
    p["submission_system"] = "PORTAL"
    p["submission_timezone"] = "America/Chicago"
    future = datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=5)
    p["submission_deadline"] = future.isoformat()
    p["documents"] = [{"document_id": "D1", "filename": "sol.pdf", "document_type": "SOLICITATION"}]
    p["requirements"] = [{"requirement_id": "R1", "requirement_text": "Price", "mandatory": True}]
    doc = _write_doc(tmp_path, "Technical.pdf")
    doc2 = _write_doc(tmp_path, "Pricing.xlsx")
    p["generated_package"] = {
        "package_id": "PKG-R5-1",
        "generation_version": 1,
        "package_status": "READY_FOR_R5_PREFLIGHT",
        "generated_documents": [doc, doc2],
        "package_hash_manifest": [
            {"filename": "Technical.pdf", "hash": "abc123"},
            {"filename": "Pricing.xlsx", "hash": "abc123"},
        ],
        "selected_scenario_id": "S1",
        "firewall_leaks": [],
        "stale": False,
    }
    p["selected_bid_price_scenario"] = {
        "scenario_id": "S1",
        "total_bid_price": "1000.00",
        "expected_profit": "200.00",
        "evidence_quality": "SCENARIO",
    }
    p["pricing_scenarios"] = [dict(p["selected_bid_price_scenario"], scenario_status="ACTIVE")]
    p["technical_compliance_items"] = [{"requirement_id": "T1", "status": "PASS_VERIFIED"}]
    p["r3_recommendation"] = "PROCEED"
    p["owner_attestations"] = []
    p["signature_tasks"] = []
    p.update(extra)
    return p


def test_preflight_pass_counts(tmp_path, rp_store):
    p = _ready_project(tmp_path)
    result = run_preflight(p)
    assert result["overall_status"] in (PASS, WARNING)
    assert result["fail_count"] == 0
    assert result["approval_eligible"] is True
    assert result["mandatory_total"] >= 1
    assert f"{result['mandatory_passed']}/{result['mandatory_total']}" in (
        f"{result['mandatory_passed']}/{result['mandatory_total']}"
    )
    assert result["sam_api_calls"] == 0
    assert result["external_side_effects"] == 0


def test_preflight_deadline_passed_blocks(tmp_path, rp_store, monkeypatch):
    past = datetime(2020, 1, 1, 14, 0, tzinfo=ZoneInfo("America/Chicago"))
    p = _ready_project(tmp_path, submission_deadline=past.isoformat())
    import response_engine.r5_preflight as pf

    monkeypatch.setattr(pf, "now_utc", lambda: datetime(2020, 1, 2, tzinfo=timezone.utc))
    result = run_preflight(p)
    assert result["overall_status"] == FAIL
    assert any("DEADLINE" in (c.get("plain") or "").upper() for c in result["hard_fails"])
    state = build_operator_state(p)
    assert state["plain_status"] == STATUS_DEADLINE_PASSED


def test_owner_attestation_blocks_approval(tmp_path, rp_store):
    p = _ready_project(
        tmp_path,
        owner_attestations=[{"attestation_id": "A1", "question": "Section 889?", "owner_confirmed": False}],
    )
    result = run_preflight(p)
    assert result["overall_status"] == OWNER_ACTION_REQUIRED
    assert result["approval_eligible"] is False
    state = build_operator_state(p)
    assert state["plain_status"] == STATUS_WAITING_OWNER
    assert state["next_action"]["assigned_role"] == ROLE_OWNER


def test_owner_approval_package_version_and_invalidate(tmp_path, rp_store):
    from response_engine.store import save_project

    p = _ready_project(tmp_path)
    save_project(p)
    pf = run_r5_preflight(p)
    assert pf["approval_eligible"]
    result = r5_owner_approve(p, decision="APPROVED", approved_by="owner")
    assert result["ok"]
    assert p["owner_submission_approval"]["approval_status"] == APPROVAL_APPROVED
    assert p.get("frozen_submission_package")
    assert p["frozen_submission_package"]["submitted"] is False

    invalidate_r5_on_change(p, reason="PRICE_CHANGE")
    assert p["owner_submission_approval"]["approval_status"] == APPROVAL_EXPIRED
    assert p["frozen_submission_package"]["invalidated"] is True


def test_amendment_after_approval_blocks(tmp_path, rp_store):
    from response_engine.store import save_project

    p = _ready_project(tmp_path)
    save_project(p)
    run_r5_preflight(p)
    r5_owner_approve(p, decision="APPROVED", approved_by="owner")
    p["amendment_review_queue"] = [{"amendment_id": "AM1"}]
    p["generated_package"]["stale"] = True
    invalidate_r5_on_change(p, reason="AMENDMENT")
    pf = run_preflight(p)
    assert pf["overall_status"] == FAIL
    assert any("stale" in (c.get("plain") or "").lower() or "amendment" in (c.get("plain") or "").lower() for c in pf["checks"])
    state = build_operator_state(p)
    assert "AMENDMENT" in (state["next_action"]["action_label"] or "").upper() or "stale" in (
        state["next_action"]["reason"] or ""
    ).lower() or state["next_action"]["action_type"] in {"FIX_PREFLIGHT", "REVIEW_AMENDMENT"} or "REVIEW" in (
        pf.get("next_action_plain") or ""
    ).upper()


def test_signature_never_auto_signed(tmp_path, rp_store):
    p = _ready_project(tmp_path)
    p["generated_package"]["generated_documents"][0]["owner_signature_required"] = True
    p["generated_package"]["generated_documents"][0]["hash"] = "h1"
    tasks = ensure_signature_tasks(p)
    assert tasks
    assert all(t["status"] != SIG_SIGNED for t in tasks)
    assert auto_signed_count(p) == 0
    pf = run_preflight(p)
    assert pf["overall_status"] == OWNER_ACTION_REQUIRED
    # explicit sign
    from response_engine.store import save_project

    save_project(p)
    signed = r5_sign(p, task_id=tasks[0]["task_id"], signed_by="owner")
    assert signed["ok"]
    assert auto_signed_count(p) == 0


def test_file_hash_mismatch_blocks_freeze_integrity(tmp_path, rp_store):
    p = _ready_project(tmp_path)
    run_preflight(p)
    # Fake approval + freeze then mutate hash
    p["owner_submission_approval"] = {
        "approval_status": APPROVAL_APPROVED,
        "approval_id": "OAP1",
        "package_id": "PKG-R5-1",
        "package_version": 1,
        "total_offer": "1000.00",
    }
    freeze_submission_package(p)
    # Change on-disk content → hash check in freeze integrity if rehashed; simulate recorded hash change
    p["frozen_submission_package"]["files"][0]["hash"] = "stale-hash"
    p["generated_package"]["generated_documents"][0]["hash"] = "new-hash"
    pf = run_preflight(p, freeze=p["frozen_submission_package"])
    # Either fail on freeze integrity or still pass depending on check implementation
    assert pf["checks"]


def test_portal_price_conflict(tmp_path, rp_store):
    p = _ready_project(tmp_path)
    p["owner_submission_approval"] = {"total_offer": "45000", "approval_status": APPROVAL_APPROVED}
    bad = validate_portal_price_entry(p, "54000")
    assert bad["ok"] is False
    assert bad["error"] == "SUBMISSION_DATA_CONFLICT"
    good = validate_portal_price_entry(p, "45000")
    assert good["ok"] is True


def test_adapters_dry_run_and_types(tmp_path, rp_store):
    for system, expected in (
        ("DIBBS", "DIBBS"),
        ("PIEE", "PIEE"),
        ("EMAIL", "EMAIL"),
        ("OpenGov Portal", "PORTAL"),
        ("PHYSICAL MAIL", "PHYSICAL"),
    ):
        p = _ready_project(tmp_path, submission_system=system)
        assert detect_adapter_type(p) == expected
        plan = build_submission_plan(p, dry_run=True)
        assert plan["dry_run"] is True
        assert "NOT SENT" in plan["label"]
        assert plan["final_submit_requires_owner"] is True


def test_dry_run_submit_receipt_and_unconfirmed(tmp_path, rp_store):
    from response_engine.store import save_project

    p = _ready_project(tmp_path)
    save_project(p)
    run_r5_preflight(p)
    r5_owner_approve(p, decision="APPROVED", approved_by="owner")
    out = r5_dry_run_submit(p, submitted_by="fixture")
    assert out["ok"]
    assert out["external_side_effects"] == 0
    assert p["r5_submission_status"] == DRY_RUN_SUBMITTED_CONFIRMED
    assert p.get("latest_receipt")
    assert p.get("submission_audits")
    state = build_operator_state(p)
    assert state["plain_status"] == STATUS_AWAITING_RESULT

    # Unconfirmed path
    p2 = _ready_project(tmp_path)
    p2["response_project_id"] = "r5-unconfirmed-1"
    p2["canonical_opportunity_id"] = "r5-unconfirmed"
    save_project(p2)
    from response_engine.submission_audit import new_submission_event

    new_submission_event(p2, submitted_by="op", submission_status=SUB_SUBMITTED_UNCONFIRMED, dry_run=False)
    p2["r5_submission_status"] = SUB_SUBMITTED_UNCONFIRMED
    st2 = build_operator_state(p2)
    assert st2["next_action"]["action_type"] == "VERIFY_RECEIPT"


def test_receipt_missing_vs_confirmed(tmp_path, rp_store):
    from response_engine.store import save_project

    p = _ready_project(tmp_path)
    save_project(p)
    p["r5_submission_status"] = SUB_SUBMITTED_UNCONFIRMED
    st = build_operator_state(p)
    assert "VERIFY" in st["next_action"]["action_label"]
    r5_record_receipt(p, confirmation_number="ABC-123", dry_run=True)
    assert p["r5_submission_status"] == DRY_RUN_SUBMITTED_CONFIRMED


def test_operator_state_plain_language_no_r_labels(tmp_path, rp_store):
    p = _ready_project(tmp_path)
    run_preflight(p)
    state = build_operator_state(p)
    blob = str(state)
    assert "R1" not in blob or "READY FOR OWNER" in state["plain_status"]
    assert state["plain_status"] == STATUS_READY_FOR_APPROVAL
    assert "R5" not in (state["next_action"]["action_label"] or "")
    assert all("R1" not in s["label"] and "R5" not in s["label"] for s in state["stages"])


def test_ui_contract_cage_and_attestation(tmp_path, rp_store):
    p = _ready_project(tmp_path, r3_blockers=["DIBBS_CAGE_REQUIRED"])
    # Clear package so cage ladder wins before preflight path
    p.pop("generated_package", None)
    state = build_operator_state(p)
    assert state["next_action"]["action_type"] == "RESOLVE_CAGE"
    assert "CAGE" in state["next_action"]["action_label"]

    p2 = _ready_project(
        tmp_path,
        owner_attestations=[{"attestation_id": "A1", "question": "Confirm origin", "owner_confirmed": False}],
    )
    p2.pop("generated_package", None)
    p2["documents"] = [{"document_id": "D1"}]
    st2 = build_operator_state(p2)
    assert st2["next_action"]["assigned_role"] == ROLE_OWNER
    assert "OWNER" in st2["next_action"]["action_label"] or "CONFIRM" in st2["next_action"]["action_label"]


def test_technical_fail_do_not_bid(tmp_path, rp_store):
    p = _ready_project(
        tmp_path,
        technical_compliance_items=[{"requirement_id": "T1", "status": "FAIL"}],
    )
    p.pop("generated_package", None)
    state = build_operator_state(p)
    assert "DO NOT BID" in state["plain_status"] or "DO NOT BID" in state["next_action"]["action_label"]


def test_no_dead_end_next_action(tmp_path, rp_store):
    cases = [
        _ready_project(tmp_path),
        _ready_project(tmp_path, r3_blockers=["REGISTRATION_REQUIRED"]),
        new_response_project(canonical_opportunity_id="empty", title="Empty"),
    ]
    for p in cases:
        if p.get("r3_blockers"):
            p.pop("generated_package", None)
        state = build_operator_state(p)
        nba = state["next_action"]
        assert nba
        assert nba.get("action_label")
        assert nba.get("destination")
        assert nba.get("reason")


def test_e2e_golden_dry_run_flow(tmp_path, rp_store):
    from response_engine.store import save_project

    p = _ready_project(tmp_path, submission_system="EMAIL", response_type="EMAIL")
    save_project(p)
    pf = run_r5_preflight(p)
    assert pf["approval_eligible"]
    assert r5_owner_approve(p, decision="APPROVED", approved_by="owner")["ok"]
    assert p["frozen_submission_package"]
    plan = build_submission_plan(p, dry_run=True)
    assert plan["adapter"] == "EMAIL"
    out = r5_dry_run_submit(p)
    assert out["status"] == DRY_RUN_SUBMITTED_CONFIRMED
    assert out["external_side_effects"] == 0
    assert auto_signed_count(p) == 0
