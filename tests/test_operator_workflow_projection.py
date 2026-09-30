"""Phase A — operator workflow projection layer tests (read-only mapping)."""

from __future__ import annotations

from operator_workflow import (
    OW_AWARDED,
    OW_BID_PREPARATION,
    OW_DEEP_RESEARCH,
    OW_DISCOVERED,
    OW_FINANCE_REVIEW,
    OW_HISTORY,
    OW_ORDERING,
    OW_PAID,
    OW_QUALIFIED,
    OW_SUBMITTED,
    OW_SUPPLIER_VALIDATION,
    map_single_source_state,
    project_operator_workflow,
)
from operator_workflow.mapping import (
    ALL_SOURCE_TABLES,
    AWARD_LIFECYCLE_MAP,
    DEAL_LIFECYCLE_MAP,
    INVENTORY_STATE_MAP,
    M3_LIFECYCLE_MAP,
    MICRO_LAB_ECON_MAP,
    MICRO_LAB_STATE_MAP,
    OPERATOR_LIFECYCLE_MAP,
    PORTFOLIO_STATE_MAP,
)


REQUIRED_KEYS = {
    "operator_workflow_state",
    "operator_state_reason",
    "operator_next_action",
    "operator_blockers",
    "confidence_level",
    "missing_information",
    "risk_flags",
    "state_conflict_detected",
}


def test_projection_shape():
    out = project_operator_workflow({"lifecycle": "DISCOVERED"})
    assert REQUIRED_KEYS.issubset(out.keys())
    assert "READY_FOR_ECONOMICS" not in out["operator_next_action"]


def test_every_mapped_source_state_resolves():
    """Every known source enum maps to an operator state (coverage)."""
    for source, table in ALL_SOURCE_TABLES.items():
        for raw in table:
            r = map_single_source_state(source, raw)
            assert r["unmapped"] is False, (source, raw)
            assert r["mapped_operator_state"] in {
                OW_DISCOVERED,
                OW_QUALIFIED,
                OW_DEEP_RESEARCH,
                OW_SUPPLIER_VALIDATION,
                OW_FINANCE_REVIEW,
                OW_BID_PREPARATION,
                OW_SUBMITTED,
                OW_AWARDED,
                OW_ORDERING,
                "DELIVERED",
                OW_PAID,
                OW_HISTORY,
            }


def test_m3_lifecycle_sample_mappings():
    assert M3_LIFECYCLE_MAP["FUNDING_VERIFICATION_REQUIRED"] == OW_FINANCE_REVIEW
    assert M3_LIFECYCLE_MAP["COMMERCIAL_VERIFICATION_REQUIRED"] == OW_SUPPLIER_VALIDATION
    assert M3_LIFECYCLE_MAP["READY_FOR_OPERATOR_ACTION"] == OW_BID_PREPARATION
    assert PORTFOLIO_STATE_MAP["COMMERCIAL_VERIFICATION_WORTHY"] == OW_SUPPLIER_VALIDATION
    assert MICRO_LAB_STATE_MAP["COMPLETE"] == OW_SUPPLIER_VALIDATION  # not SUBMITTED
    assert OPERATOR_LIFECYCLE_MAP["COMPLETE"] == OW_HISTORY  # OP_COMPLETE = closed
    assert AWARD_LIFECYCLE_MAP["PERFORMING"] == OW_ORDERING
    assert DEAL_LIFECYCLE_MAP["PAID"] == OW_PAID
    assert INVENTORY_STATE_MAP["SUBMITTED"] == OW_SUBMITTED
    assert MICRO_LAB_ECON_MAP["BID_CANDIDATE"] == OW_BID_PREPARATION


def test_unknown_economics_cannot_be_bid_preparation():
    out = project_operator_workflow(
        {
            "lifecycle": "READY_FOR_OPERATOR_ACTION",
            "Deal_state": "OWNER_REVIEW",
            "pricing_level": "LEVEL_4_UNKNOWN",
            "economics_unknown": True,
            "supplier_identified": True,
            "current_market_seller": "ACME Dist",
            "funding_resolved": True,
        }
    )
    assert out["operator_workflow_state"] != OW_BID_PREPARATION
    assert out["operator_workflow_state"] in {OW_DEEP_RESEARCH, OW_SUPPLIER_VALIDATION, OW_FINANCE_REVIEW}
    assert "ECONOMICS_UNKNOWN" in out["operator_blockers"]
    assert out["operator_workflow_state"] == OW_DEEP_RESEARCH


def test_unknown_supplier_cannot_be_bid_ready():
    out = project_operator_workflow(
        {
            "lifecycle": "DRAFT_BID_READY",
            "Deal_state": "OWNER_REVIEW",
            "supplier_identified": False,
            "Gross_spread": 5000,
            "last_government_unit_price": 100,
            "current_public_price": None,
            "economics_supported": True,
            "expected_actual_profit": 5000,
            "funding_resolved": True,
            "economics_unknown": False,
        }
    )
    assert out["operator_workflow_state"] != OW_BID_PREPARATION
    assert out["operator_workflow_state"] == OW_SUPPLIER_VALIDATION
    assert "SUPPLIER_UNKNOWN" in out["operator_blockers"]
    assert "supplier" in out["operator_next_action"].lower()


def test_conflicting_ready_vs_verify_resolves_conservatively():
    out = project_operator_workflow(
        {
            "lifecycle": "READY_FOR_OPERATOR_ACTION",
            "Funding": "VERIFY",
            "Deal_state": "COMMERCIAL_VERIFICATION_WORTHY",
            "pricing_level": "LEVEL_4_UNKNOWN",
            "supplier_identified": False,
        }
    )
    assert out["state_conflict_detected"] is True
    assert out["conflicting_states"]
    assert out["operator_workflow_state"] in {OW_DEEP_RESEARCH, OW_FINANCE_REVIEW, OW_SUPPLIER_VALIDATION}
    assert out["operator_workflow_state"] != OW_BID_PREPARATION


def test_audit_example_verification_worthy_plus_funding_plus_unknown_econ():
    """Conservative: LEVEL_4_UNKNOWN pulls to DEEP_RESEARCH (not BID_PREPARATION)."""
    out = project_operator_workflow(
        {
            "Deal_state": "COMMERCIAL_VERIFICATION_WORTHY",
            "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
            "pricing_level": "LEVEL_4_UNKNOWN",
            "Funding": "VERIFY",
        }
    )
    assert out["operator_workflow_state"] == OW_DEEP_RESEARCH
    assert out["state_conflict_detected"] is True
    assert "ECONOMICS_UNKNOWN" in out["operator_blockers"] or "LEVEL_4" in str(out.get("source_signals"))


def test_funding_only_lands_in_finance_review():
    out = project_operator_workflow(
        {
            "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
            "Deal_state": "FUNDING_VERIFICATION_REQUIRED",
            "supplier_identified": True,
            "current_market_seller": "Dist Co",
            "current_public_price": 50,
            "last_government_unit_price": 80,
            "Gross_spread": 3000,
            "economics_unknown": False,
            "funding_unresolved": True,
        }
    )
    assert out["operator_workflow_state"] == OW_FINANCE_REVIEW
    assert "financ" in out["operator_next_action"].lower()


def test_full_evidence_allows_bid_preparation():
    out = project_operator_workflow(
        {
            "lifecycle": "READY_FOR_OPERATOR_ACTION",
            "Deal_state": "OWNER_REVIEW",
            "supplier_identified": True,
            "current_market_seller": "Authorized Dist",
            "current_public_price": 220,
            "last_government_unit_price": 327,
            "Gross_spread": 1200,
            "economics_supported": True,
            "expected_actual_profit": 1200,
            "funding_resolved": True,
            "funding_path_identified": True,
            "economics_unknown": False,
        }
    )
    assert out["operator_workflow_state"] == OW_BID_PREPARATION
    assert "ECONOMICS_UNKNOWN" not in out["operator_blockers"]
    assert "SUPPLIER_UNKNOWN" not in out["operator_blockers"]


def test_award_lifecycle_authoritative_for_post_submit():
    out = project_operator_workflow(
        {
            "lifecycle": "RESEARCH_QUEUED",  # stale pre-award signal
            "award_lifecycle_status": "PERFORMING",
        }
    )
    assert out["operator_workflow_state"] == OW_ORDERING


def test_paid_and_history():
    assert project_operator_workflow({"award_lifecycle": {"lifecycle_status": "PAID"}})[
        "operator_workflow_state"
    ] == OW_PAID
    assert project_operator_workflow({"lifecycle": "ARCHIVED"})["operator_workflow_state"] == OW_HISTORY
    assert project_operator_workflow({"lifecycle": "REJECTED_CHEAP_SCREEN"})[
        "operator_workflow_state"
    ] == OW_HISTORY


def test_micro_lab_complete_is_not_submitted():
    out = project_operator_workflow(
        {
            "research_state": "COMPLETE",
            "supplier_identified": True,
            "current_market_seller": "Dist",
            "current_public_price": 10,
            "last_government_unit_price": 12,
            "Gross_spread": 100,
            "funding_resolved": True,
            "economics_unknown": False,
        }
    )
    assert out["operator_workflow_state"] != OW_SUBMITTED
    assert "MICRO_LAB_COMPLETE_IS_NOT_BID_SUBMITTED" in out["risk_flags"] or out[
        "operator_workflow_state"
    ] in {OW_SUPPLIER_VALIDATION, OW_BID_PREPARATION, OW_FINANCE_REVIEW}


def test_qualified_from_cheap_screen():
    out = project_operator_workflow({"lifecycle": "CHEAP_SCREENED"})
    assert out["operator_workflow_state"] == OW_QUALIFIED


def test_existing_lifecycle_module_unchanged():
    """Projection must not alter m3_lifecycle exports or derive behavior."""
    import m3_lifecycle as lc

    assert lc.LC_FUNDING_VERIFICATION == "FUNDING_VERIFICATION_REQUIRED"
    row = {"title": "Widget", "external_id": "x1"}
    assert lc.derive_lifecycle(row) == "DISCOVERED"
    # project does not mutate
    raw = {"lifecycle": "BOM_READY", "title": "x"}
    before = dict(raw)
    project_operator_workflow(raw)
    assert raw == before


def test_human_next_action_not_internal_enum():
    out = project_operator_workflow({"lifecycle": "FUNDING_VERIFICATION_REQUIRED", "funding_unresolved": True})
    assert "FUNDING_VERIFICATION_REQUIRED" not in out["operator_next_action"]
    assert "financ" in out["operator_next_action"].lower()
