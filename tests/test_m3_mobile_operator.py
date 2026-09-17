"""Focused tests — M3 mobile operator read-models and APIs."""

from __future__ import annotations

from pathlib import Path

from m3_mobile_read_model import (
    action_queue_mobile,
    deal_room_summary,
    mobile_dashboard_summary,
    opportunity_card_summary,
)
from m3_pipeline_store import M3PipelineStore
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, is_development_no_outreach, set_operating_mode


def _sample_row(**overrides):
    base = {
        "canonical_id": "mob-001",
        "agency": "School District Computers Buyer",
        "title": "School District Computers",
        "solicitation_number": "SD-PC-09",
        "source_id": "bidnet_demo",
        "deadline": "2099-10-01",
        "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
        "package_access": "PUBLIC",
        "product_category": "computers",
        "product_classification": "IT_HARDWARE",
        "cheap_screen_survive": True,
        "operator_readiness": "FUNDING_VERIFICATION_REQUIRED",
        "deadline_evaluation": {"calendar_days_remaining": 9},
        "line_items": [
            {"description": "Laptop", "quantity": 100, "unit": "EA", "part_number": "UNKNOWN"}
        ],
        "transaction_economics": {},
        "funding_status": "FUNDING_VERIFICATION_REQUIRED",
        "funding_requirement": {"status": "FUNDING_VERIFICATION_REQUIRED", "capital_amount": None},
        "bid_compliance": {"unresolved": ["bond_unknown"], "mandatory_unresolved": ["insurance"]},
        "operator_actions": [
            {
                "action_type": "Obtain supplier quote",
                "why_needed": "Profit depends on acquisition cost",
                "priority": 10,
                "deadline": "2099-09-25",
                "status": "OPEN",
                "unlocks_pipeline_stage": "COMMERCIAL_VERIFICATION",
            }
        ],
        "pending_next_action": {
            "next_action": "WAIT_FUNDING_VERIFICATION",
            "reason": "Verify financing path",
            "note": "UNKNOWN financing ≠ rejection",
        },
        "stop_reason": None,
    }
    base.update(overrides)
    return base


def test_opportunity_summary_preserves_unknown_economics():
    card = opportunity_card_summary(_sample_row())
    assert card["supported_revenue"] == "UNKNOWN"
    assert card["supported_profit"] == "UNKNOWN"
    assert card["profit_confidence"] == "UNKNOWN"
    assert card["funding_state"] == "FUNDING_VERIFICATION_REQUIRED"
    assert card["next_action"] == "WAIT_FUNDING_VERIFICATION"
    assert "buyer" in card and card["title"] == "School District Computers"


def test_opportunity_summary_does_not_fabricate_when_partial():
    card = opportunity_card_summary(
        _sample_row(
            transaction_economics={"revenue": 285000, "confidence": "Medium"},
            expected_actual_profit=None,
        )
    )
    assert card["supported_revenue"] == 285000.0
    assert card["supported_profit"] == "UNKNOWN"
    assert card["profit_confidence"] == "Medium"


def test_deal_room_sections_and_unknown_funding(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    deal = deal_room_summary(_sample_row())
    assert deal["kind"] == "M3DealRoom"
    for key in (
        "overview",
        "product_fit",
        "requirements",
        "economics",
        "funding",
        "compliance",
        "pricing",
        "actions",
    ):
        assert key in deal
    assert deal["economics"]["expected_profit"] == "UNKNOWN"
    assert deal["economics"]["note"]
    assert deal["funding"]["unknown_financing_is_not_rejection"] is True
    assert "eligibility" in deal["funding"]["unknowns"]
    assert deal["DEVELOPMENT_NO_OUTREACH"] is True
    assert is_development_no_outreach()
    assert "bond_unknown" in (deal["compliance"]["blockers"] or [])
    assert deal["requirements"]["bom_lines"][0]["quantity"] == 100


def test_action_queue_priority_and_missing_evidence(tmp_path):
    store = M3PipelineStore(path=tmp_path / "mob.json")
    store._rows["mob-001"] = _sample_row()
    store.save()
    store = M3PipelineStore(path=tmp_path / "mob.json")
    q = action_queue_mobile(store)
    assert q["question"] == "WHAT DO I NEED TO DO?"
    assert q["count"] >= 1
    a = q["actions"][0]
    assert a["opportunity_title"]
    assert a["need"]
    assert a["reason"]
    assert a["deadline"]
    assert a["missing_evidence"]
    assert a["expected_impact"]
    assert a["priority_label"] in {"HIGH PRIORITY", "MEDIUM PRIORITY", "NORMAL"}
    assert q["DEVELOPMENT_NO_OUTREACH"] is True


def test_mobile_dashboard_payload_compact(tmp_path):
    store = M3PipelineStore(path=tmp_path / "dash.json")
    store._rows["mob-001"] = _sample_row(
        transaction_economics={
            "revenue": 285000,
            "expected_actual_profit": 28000,
            "confidence": "Medium",
        },
        expected_actual_profit=28000,
    )
    store._rows["mob-rej"] = _sample_row(
        canonical_id="mob-rej",
        lifecycle="REJECTED_CHEAP_SCREEN",
        title="Rejected",
    )
    store.save()
    dash = mobile_dashboard_summary(M3PipelineStore(path=tmp_path / "dash.json"))
    assert dash["kind"] == "M3MobileDashboard"
    assert dash["active_count"] == 1
    opp = dash["active_opportunities"][0]
    assert opp["supported_revenue"] == 285000.0
    assert opp["supported_profit"] == 28000.0
    assert "payload_note" in dash


def test_deadline_risk_ranks_higher(tmp_path):
    store = M3PipelineStore(path=tmp_path / "rank.json")
    store._rows["soon"] = _sample_row(
        canonical_id="soon",
        title="Urgent",
        deadline_evaluation={"calendar_days_remaining": 2},
        pending_next_action={"next_action": "WAIT_COMMERCIAL_VERIFICATION", "reason": "quote"},
    )
    store._rows["later"] = _sample_row(
        canonical_id="later",
        title="Later",
        deadline_evaluation={"calendar_days_remaining": 40},
        pending_next_action={"next_action": "WAIT_COMMERCIAL_VERIFICATION", "reason": "quote"},
        operator_actions=[],
    )
    store.save()
    dash = mobile_dashboard_summary(M3PipelineStore(path=tmp_path / "rank.json"))
    ids = [c["canonical_id"] for c in dash["active_opportunities"]]
    assert ids.index("soon") < ids.index("later")


def test_mobile_api_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    path = tmp_path / "api_store.json"
    store = M3PipelineStore(path=path)
    store._rows["api-1"] = _sample_row(canonical_id="api-1")
    store.save()

    import m3_mobile_read_model as mm
    import m3_pipeline_store as mps

    monkeypatch.setattr(mps, "DEFAULT_PATH", path)
    monkeypatch.setattr(mm, "M3PipelineStore", lambda path=None: M3PipelineStore(path=path or mps.DEFAULT_PATH))

    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    dash = client.get("/api/m3/mobile/dashboard")
    assert dash.status_code == 200
    body = dash.json()
    assert body["kind"] == "M3MobileDashboard"
    assert "active_opportunities" in body

    opps = client.get("/api/m3/mobile/opportunities")
    assert opps.status_code == 200
    assert "opportunities" in opps.json()

    acts = client.get("/api/m3/mobile/actions")
    assert acts.status_code == 200
    assert acts.json()["question"] == "WHAT DO I NEED TO DO?"

    deal = client.get("/api/m3/mobile/deal/api-1")
    assert deal.status_code == 200
    assert deal.json()["overview"]["title"]
    assert deal.json()["economics"]["expected_profit"] in ("UNKNOWN", 28000, 28000.0) or True
    # Never fabricate: missing profit stays UNKNOWN for this fixture
    assert deal.json()["economics"]["expected_profit"] == "UNKNOWN"

    missing = client.get("/api/m3/mobile/deal/does-not-exist")
    assert missing.status_code == 404

    sources = client.get("/api/m3/mobile/sources")
    assert sources.status_code == 200
    assert sources.json()["claim_100_percent_coverage"] is False


def test_static_mobile_assets_present():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "m3-mobile.js").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    assert 'id="view-m3-home"' in html
    assert 'id="view-m3-deal-room"' in html
    assert 'id="m3-bottom-nav"' in html
    assert "m3-mobile.js" in html
    assert "showM3View" in js
    assert "openDealRoom" in js
    assert "M3 Mobile Operator Experience" in css
    assert ".m3-bottom-nav" in css
    assert ".m3-opp-card" in css
    audit = Path(__file__).resolve().parents[1] / "artifacts" / "mobile_ui_audit.json"
    assert audit.exists()
