"""Phase B — operator workflow fields on UI-facing API/read-models."""

from __future__ import annotations

from m3_mobile_read_model import (
    deal_room_summary,
    mobile_dashboard_summary,
    opportunity_card_summary,
)
from operator_workflow.summary import (
    OPERATOR_FIELD_KEYS,
    attach_operator_workflow,
    build_operator_dashboard_payload,
    build_operator_deal_summary,
)
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode


def _row(**overrides):
    base = {
        "canonical_id": "ow-api-001",
        "agency": "Test Agency",
        "title": "DEWALT Drill Kit Resale",
        "solicitation_number": "T-1",
        "deadline": "2099-12-01",
        "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
        "Deal_state": "COMMERCIAL_VERIFICATION_WORTHY",
        "Funding": "VERIFY",
        "pricing_level": "LEVEL_4_UNKNOWN",
        "deadline_evaluation": {"calendar_days_remaining": 14},
        "estimated_value": 4500,
        "operator_readiness": "FUNDING_VERIFICATION_REQUIRED",
        "funding_status": "FUNDING_VERIFICATION_REQUIRED",
    }
    base.update(overrides)
    return base


def test_operator_deal_summary_answers_operator_questions():
    s = build_operator_deal_summary(_row())
    assert s["kind"] == "OperatorDealSummary"
    assert s["stage"] == s["operator_workflow_state"]
    assert s["what_next"] == s["operator_next_action"]
    assert s["has_contradiction"] == s["state_conflict_detected"]
    assert s["blocking_progress"] == s["operator_blockers"]
    assert s["operator_workflow_state"] != "BID_PREPARATION"
    assert "LEVEL_4" in str(s.get("source_signals") or s.get("operator_state_reason")) or "ECONOMICS" in str(
        s.get("operator_blockers")
    )


def test_attach_preserves_existing_keys():
    payload = {"canonical_id": "x", "lifecycle": "BOM_READY", "custom": 1}
    out = attach_operator_workflow(payload, _row(lifecycle="BOM_READY"), include_summary=True)
    assert out["custom"] == 1
    assert out["lifecycle"] == "BOM_READY"
    for k in OPERATOR_FIELD_KEYS:
        assert k in out
    assert "operator_summary" in out


def test_opportunity_card_includes_operator_fields():
    card = opportunity_card_summary(_row())
    for k in OPERATOR_FIELD_KEYS:
        assert k in card
    assert card["lifecycle"] == "FUNDING_VERIFICATION_REQUIRED"  # existing field preserved
    assert card["supported_revenue"] == "UNKNOWN"
    assert card["operator_workflow_state"] != "BID_PREPARATION"


def test_deal_room_includes_operator_fields():
    room = deal_room_summary(_row())
    assert room["kind"] == "M3DealRoom"
    for k in OPERATOR_FIELD_KEYS:
        assert k in room
    assert room["overview"]["lifecycle"] == "FUNDING_VERIFICATION_REQUIRED"
    assert room["overview"].get("operator_workflow_state") == room["operator_workflow_state"]
    assert room["state_conflict_detected"] is True or "ECONOMICS_UNKNOWN" in room["operator_blockers"]


def test_dashboard_operator_prep_structure():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    payload = build_operator_dashboard_payload(
        [
            _row(),
            _row(
                canonical_id="ow-api-002",
                lifecycle="READY_FOR_OPERATOR_ACTION",
                Deal_state="OWNER_REVIEW",
                pricing_level="LEVEL_4_UNKNOWN",
                economics_unknown=True,
                supplier_identified=True,
                funding_resolved=True,
            ),
        ]
    )
    assert payload["kind"] == "OperatorDashboardPrep"
    assert "active_work" in payload
    assert "attention_needed" in payload
    assert "unknown_economics" in payload["attention_needed"]
    # Unknown economics must not appear as completed / bid prep in active work
    for item in payload["active_work"]:
        assert item["operator_workflow_state"] != "BID_PREPARATION"


def test_dashboard_summary_includes_operator_dashboard_key(monkeypatch):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    class _FakeStore:
        def reload_from_durable(self):
            return None

        def all(self):
            return [_row()]

        def operator_queue(self):
            return []

    dash = mobile_dashboard_summary(_FakeStore())
    assert "operator_dashboard" in dash
    assert dash["kind"] == "M3MobileDashboard"
    # Existing keys still present
    assert "active_opportunities" in dash
    assert "top_actions" in dash
    # Cards carry operator fields when present
    if dash["active_opportunities"]:
        card = dash["active_opportunities"][0]
        assert "operator_workflow_state" in card


def test_conflicts_remain_visible_on_card():
    card = opportunity_card_summary(
        _row(
            lifecycle="READY_FOR_OPERATOR_ACTION",
            Funding="VERIFY",
            pricing_level="LEVEL_4_UNKNOWN",
        )
    )
    assert card["state_conflict_detected"] is True
    assert card["operator_blockers"] or (card.get("operator_summary") or {}).get("conflicting_states")


def test_existing_lifecycle_derive_unchanged():
    from m3_lifecycle import derive_lifecycle

    row = {"title": "x", "external_id": "1"}
    assert derive_lifecycle(row) == "DISCOVERED"
    # attaching operator fields does not mutate source row
    raw = _row()
    before = dict(raw)
    attach_operator_workflow({"title": "t"}, raw)
    assert raw == before
