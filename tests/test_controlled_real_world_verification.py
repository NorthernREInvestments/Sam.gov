"""Focused tests — controlled real-world verification + transaction learning."""

from __future__ import annotations

from pathlib import Path

from commercial_verification_constants import (
    ACT_FINANCING_IND,
    ACT_REGISTER,
    ACT_SUBMIT_BID,
    ACT_SUPPLIER_QUOTE,
    EXEC_BLOCKED_FORBIDDEN,
    EXEC_BLOCKED_MODE,
    EXEC_CONTROLLED_AUTHORIZED,
)
from external_action_control import (
    execute_external_action,
    get_external_action_store,
    reset_external_action_store,
)
from first_pursuit_selection import rank_first_pursuits, score_first_pursuit_candidate
from learning_feedback import (
    apply_feedback_to_pursuit_score,
    compute_learning_feedback,
    first_five_contract_learning_report,
)
from live_review_package import live_opportunity_review_package
from m3_pipeline_store import M3PipelineStore
from operating_mode import (
    MODE_CONTROLLED_REAL_WORLD_VERIFICATION,
    MODE_DEVELOPMENT_NO_OUTREACH,
    disable_controlled_real_world_verification,
    enable_controlled_real_world_verification,
    is_controlled_verification,
    is_development_no_outreach,
    mode_snapshot,
    outreach_allowed,
    set_operating_mode,
)
from transaction_learning import TransactionLearningStore, reset_transaction_learning_store


def _row(**over):
    base = {
        "canonical_id": "crwv-001",
        "agency": "City Equipment Buyer",
        "title": "City Equipment Purchase",
        "source_id": "bidnet_demo",
        "package_access": "PUBLIC",
        "product_category": "IT_HARDWARE",
        "product_classification": "computers",
        "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
        "deadline_evaluation": {"calendar_days_remaining": 14},
        "line_items": [
            {"description": "Laptop", "quantity": 50, "unit": "EA", "part_number": "X1"}
        ],
        "transaction_economics": {"revenue": 285000, "confidence": "Medium"},
        "government_revenue": 285000,
        "funding_status": "UNKNOWN",
        "funding_requirement": {"status": "UNKNOWN", "capital_amount": 40000},
        "bid_compliance": {"unresolved": [], "mandatory_unresolved": []},
        "cheap_screen_survive": True,
    }
    base.update(over)
    return base


def test_controlled_mode_requires_explicit_authorization():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    assert is_development_no_outreach()
    assert not is_controlled_verification()
    denied = enable_controlled_real_world_verification(operator_id="op1", acknowledgment=False)
    assert denied.get("enabled") is False
    assert not is_controlled_verification()
    ok = enable_controlled_real_world_verification(operator_id="op1", acknowledgment=True)
    assert ok["controlled_verification_active"] is True
    assert ok["operating_mode"] == MODE_CONTROLLED_REAL_WORLD_VERIFICATION
    assert outreach_allowed() is False
    assert ok["automatic_external_actions_allowed"] is False
    disable_controlled_real_world_verification(operator_id="op1")
    assert is_development_no_outreach()
    assert not is_controlled_verification()


def test_dev_mode_still_blocks_even_when_authorized():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()
    store = get_external_action_store()
    a = store.propose(
        opportunity_id="X1",
        action_type=ACT_SUPPLIER_QUOTE,
        purpose="quote",
        target="supplier@example.com",
    )
    store.authorize(a["action_id"], operator_id="op")
    r = execute_external_action(a["action_id"])
    assert r["execution_state"] == EXEC_BLOCKED_MODE
    assert r["network_transmitted"] is False
    assert r["quote_requests_made"] == 0
    assert r["bids_submitted"] == 0


def test_controlled_allows_authorized_verification_only_no_network():
    enable_controlled_real_world_verification(operator_id="op", acknowledgment=True)
    reset_external_action_store()
    store = get_external_action_store()
    a = store.propose(
        opportunity_id="X1",
        action_type=ACT_SUPPLIER_QUOTE,
        purpose="quote",
        target="supplier@example.com",
    )
    # Without auth → blocked
    blocked = execute_external_action(a["action_id"])
    assert blocked["execution_state"] != EXEC_CONTROLLED_AUTHORIZED
    store.authorize(a["action_id"], operator_id="op")
    # Reset execution for fresh run after auth
    a2 = store.get(a["action_id"])
    a2["execution_state"] = "NOT_STARTED"
    a2.pop("dry_run_result", None)
    r = execute_external_action(a["action_id"])
    assert r["execution_state"] == EXEC_CONTROLLED_AUTHORIZED
    assert r["network_transmitted"] is False
    assert r["automatic_outreach"] is False
    assert r["emails_sent"] == 0
    assert r["bids_submitted"] == 0

    # Forbidden: bid / register even if authorized
    bid = store.propose(opportunity_id="X1", action_type=ACT_SUBMIT_BID, purpose="bid")
    store.authorize(bid["action_id"], operator_id="op")
    br = execute_external_action(bid["action_id"])
    assert br["execution_state"] == EXEC_BLOCKED_FORBIDDEN
    assert br["bids_submitted"] == 0

    reg = store.propose(opportunity_id="X1", action_type=ACT_REGISTER, purpose="register")
    store.authorize(reg["action_id"], operator_id="op")
    rr = execute_external_action(reg["action_id"])
    assert rr["execution_state"] == EXEC_BLOCKED_FORBIDDEN
    assert rr["registrations"] == 0

    fin = store.propose(opportunity_id="X1", action_type=ACT_FINANCING_IND, purpose="fin")
    store.authorize(fin["action_id"], operator_id="op")
    fr = execute_external_action(fin["action_id"])
    assert fr["execution_state"] == EXEC_CONTROLLED_AUTHORIZED
    assert fr["financing_applications"] == 0
    disable_controlled_real_world_verification()


def test_first_pursuit_selection_prefers_repeatability(tmp_path):
    store = M3PipelineStore(path=tmp_path / "p.json")
    store._rows["good"] = _row(canonical_id="good")
    store._rows["huge"] = _row(
        canonical_id="huge",
        title="Mega Complex",
        transaction_economics={"revenue": 5_000_000},
        government_revenue=5_000_000,
        line_items=[{"description": f"item{i}", "quantity": 1} for i in range(20)],
        bid_compliance={"unresolved": ["a", "b", "c", "d"], "mandatory_unresolved": ["x"]},
        package_access="AUTH_GATED",
        deadline_evaluation={"calendar_days_remaining": 3},
    )
    store.save()
    ranked = rank_first_pursuits(M3PipelineStore(path=tmp_path / "p.json"), limit=5)
    ids = [c["canonical_id"] for c in ranked["candidates"]]
    assert ids[0] == "good"
    scored = score_first_pursuit_candidate(store.get("good"))
    assert scored["recommend_for_first_pursuit"] is True
    assert any(f["factor"] == "reasonable_transaction_size" for f in scored["factors"])


def test_live_review_package_sections(tmp_path):
    store = M3PipelineStore(path=tmp_path / "r.json")
    store._rows["crwv-001"] = _row()
    store.save()
    pkg = live_opportunity_review_package(canonical_id="crwv-001", store=M3PipelineStore(path=tmp_path / "r.json"))
    for key in ("overview", "product_fit", "requirements", "economics", "funding", "compliance", "next_actions"):
        assert key in pkg
    assert pkg["economics"]["note"]
    assert pkg["funding"]["unknown_financing_is_not_rejection"] is True
    assert "automatic bid submission" in " ".join(pkg["next_actions"]["forbidden_without_future_production_mode"])


def test_transaction_learning_supplier_financing_outcome(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    store = TransactionLearningStore(path=tmp_path / "tl.json")
    rec = store.start_pursuit(
        "crwv-001",
        source="bidnet_demo",
        category="computers",
        why_discovered="test",
        why_pursued="first_pursuit",
        estimated_acquisition_cost=250000,
    )
    # Blocked in pure development
    blocked = store.record_supplier_verification(
        rec["record_id"],
        supplier="ACME",
        product="Laptop",
        price=240000,
        authorized_by="op",
    )
    assert blocked.get("error") == "blocked_by_operating_mode"

    enable_controlled_real_world_verification(operator_id="op", acknowledgment=True)
    no_auth = store.record_supplier_verification(
        rec["record_id"], supplier="ACME", product="Laptop", price=240000, authorized_by=None
    )
    assert no_auth.get("error") == "authorization_required"

    ok = store.record_supplier_verification(
        rec["record_id"],
        supplier="ACME",
        product="Laptop",
        quote_date="2026-09-16",
        price=240000,
        availability="in_stock",
        lead_time="5 days",
        warranty="1 year",
        authorized_by="op",
        freshness_days=14,
    )
    assert ok.get("error") is None
    assert ok["supplier_verifications"][0]["assumed_permanent"] is False
    assert ok["supplier_verifications"][0]["network_transmitted"] is False
    assert ok["pricing"]["actual_acquisition_cost"] == 240000.0
    assert ok["pricing"]["variance"] == -10000.0

    fin = store.record_financing_verification(
        rec["record_id"],
        financing_path="PO_financing",
        result="UNKNOWN",
        state="UNKNOWN",
        authorized_by="op",
    )
    assert fin["financing"]["state"] == "UNKNOWN"
    assert fin["financing"]["unknown_is_not_rejection"] is True
    assert fin["financing_verifications"][0]["financing_application_submitted"] is False

    out = store.record_outcome(
        rec["record_id"],
        status="LOST",
        revenue=285000,
        actual_profit="UNKNOWN",
        problems=["late quote"],
        operator_id="op",
    )
    assert out["outcome"]["won_lost"] == "LOST"
    disable_controlled_real_world_verification()


def test_learning_feedback_explainable(tmp_path):
    store = TransactionLearningStore(path=tmp_path / "fb.json")
    rec = store.start_pursuit("a", source="srcA", category="computers", why_pursued="x")
    enable_controlled_real_world_verification(operator_id="op", acknowledgment=True)
    store.record_supplier_verification(
        rec["record_id"], supplier="S", product="P", price=100, authorized_by="op"
    )
    store.update_section(rec["record_id"], "pricing", {"estimated_acquisition_cost": 90, "actual_acquisition_cost": 100})
    store.record_outcome(rec["record_id"], status="WON", revenue=150, actual_profit=40, operator_id="op")
    fb = compute_learning_feedback(store)
    assert fb["black_box_scoring"] is False
    assert fb["records_analyzed"] >= 1
    assert any(a["target"] == "source_ranking" for a in fb["adjustments"])
    adj = apply_feedback_to_pursuit_score(50, source_id="srcA", category="computers", feedback=fb)
    assert adj["explainable"] is True
    assert "adjusted_score" in adj
    report = first_five_contract_learning_report(store)
    assert report["transactions_recorded"] >= 1
    assert report["slots_remaining"] <= 5
    disable_controlled_real_world_verification()


def test_no_unauthorized_actions_counts(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    snap = mode_snapshot()
    assert snap["emails_sent"] == 0
    assert snap["calls_placed"] == 0
    assert snap["supplier_contacts"] == 0
    assert snap["financier_contacts"] == 0
    assert snap["agency_contacts"] == 0
    assert snap["portal_registrations"] == 0
    assert snap["bids_submitted"] == 0
    assert snap["financing_applications"] == 0
    assert snap["signatures"] == 0
    assert snap["network_transmitted"] is False
    assert snap["outreach_allowed"] is False


def test_mobile_workflow_assets_and_apis(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    path = tmp_path / "api.json"
    store = M3PipelineStore(path=path)
    store._rows["api-1"] = _row(canonical_id="api-1")
    store.save()
    tl_path = tmp_path / "tl_api.json"
    reset_transaction_learning_store(path=tl_path)

    import m3_pipeline_store as mps
    import first_pursuit_selection as fps
    import live_review_package as lrp

    monkeypatch.setattr(mps, "DEFAULT_PATH", path)
    monkeypatch.setattr(fps, "M3PipelineStore", lambda path=None: M3PipelineStore(path=path or mps.DEFAULT_PATH))
    monkeypatch.setattr(lrp, "M3PipelineStore", lambda path=None: M3PipelineStore(path=path or mps.DEFAULT_PATH))

    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/api/m3/controlled/mode").status_code == 200
    en = client.post(
        "/api/m3/controlled/enable",
        json={"operator_id": "op", "acknowledgment": True},
    )
    assert en.status_code == 200
    assert en.json()["controlled_verification_active"] is True

    pursuits = client.get("/api/m3/controlled/first-pursuits")
    assert pursuits.status_code == 200
    assert pursuits.json()["kind"] == "M3FirstPursuitSelection"

    review = client.get("/api/m3/controlled/review/api-1")
    assert review.status_code == 200
    assert "next_actions" in review.json()

    start = client.post(
        "/api/m3/learning/start",
        json={"canonical_id": "api-1", "source": "bidnet_demo", "why_pursued": "test"},
    )
    assert start.status_code == 200
    rid = start.json()["record_id"]

    sv = client.post(
        "/api/m3/learning/supplier-verification",
        json={
            "record_id": rid,
            "supplier": "ACME",
            "product": "Laptop",
            "price": 1000,
            "authorized_by": "op",
        },
    )
    assert sv.status_code == 200

    fv = client.post(
        "/api/m3/learning/financing-verification",
        json={
            "record_id": rid,
            "financing_path": "PO",
            "result": "UNKNOWN",
            "state": "UNKNOWN",
            "authorized_by": "op",
        },
    )
    assert fv.status_code == 200
    assert fv.json()["financing"]["state"] == "UNKNOWN"

    assert client.get("/api/m3/learning/feedback").status_code == 200
    assert client.get("/api/m3/learning/first-five").status_code == 200
    client.post("/api/m3/controlled/disable", json={"operator_id": "op"})

    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="view-m3-verify"' in html
    assert "loadVerify" in js
    assert "/api/m3/controlled/enable" in js
    assert "/api/m3/learning/outcome" in js
