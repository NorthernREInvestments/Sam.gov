"""Eligibility / vehicle / access gate tests — BOAST P0 regression."""

from __future__ import annotations

from eligibility_gate import (
    COMPLETE_BOA_ONRAMP,
    ELIGIBLE_CONFIRMED,
    ELIGIBILITY_NOT_APPLICABLE,
    ELIGIBILITY_UNKNOWN,
    ELIGIBLE_CONDITIONAL,
    NOT_CURRENTLY_ELIGIBLE,
    STATUS_HELD,
    STATUS_MISSING,
    blocks_quote_outreach,
    detect_vehicle_requirements,
    evaluate_eligibility_gate,
    load_company_eligibility_profile,
)
from phase_h.deep_research import STATE_ELIGIBILITY_ACTION, STATE_QUOTE_OUTREACH, _phase_h_readiness


BOAST_TITLE = "BOAST RFOP - Panel, Power Distribution - NSN: 6110-01-082-8958"
BOAST_DESC = (
    "This Request for Order Proposal (RFOP) is limited to active BOAST Basic Ordering "
    "Agreement holders. Offerors must hold an active BOAST BOA by the closing date."
)


def test_boast_detected_from_title_and_description():
    found = detect_vehicle_requirements(f"{BOAST_TITLE}\n{BOAST_DESC}")
    assert any(v["code"] == "BOAST_BOA" for v in found)


def test_boast_not_held_blocks_quote_ready():
    gate = evaluate_eligibility_gate(
        {"title": BOAST_TITLE, "description": BOAST_DESC},
        profile={"vehicles_held": [], "jcp_status": "UNKNOWN", "source": "test"},
    )
    assert gate["overall_status"] == NOT_CURRENTLY_ELIGIBLE
    assert gate["boa_required"] is True
    assert gate["boa_status"] == STATUS_MISSING
    assert gate["actionable_for_quote_or_bid"] is False
    assert blocks_quote_outreach(gate) is True
    assert "BOAST" in (gate.get("next_action") or "").upper()
    assert gate.get("operator_state") == COMPLETE_BOA_ONRAMP


def test_boast_held_confirms_eligibility():
    gate = evaluate_eligibility_gate(
        {"title": BOAST_TITLE, "description": BOAST_DESC},
        profile={"vehicles_held": ["BOAST_BOA"], "jcp_status": "UNKNOWN", "source": "test"},
    )
    assert gate["overall_status"] == ELIGIBLE_CONFIRMED
    assert gate["boa_status"] == STATUS_HELD
    assert gate["actionable_for_quote_or_bid"] is True
    assert blocks_quote_outreach(gate) is False


def test_idiq_holder_only_not_held():
    text = "This task order competition is limited to existing IDIQ holders only."
    gate = evaluate_eligibility_gate(
        {"title": "Task Order", "description": text},
        profile={"vehicles_held": [], "source": "test"},
    )
    assert gate["idiq_holder_required"] is True
    assert gate["actionable_for_quote_or_bid"] is False
    assert gate["overall_status"] in {NOT_CURRENTLY_ELIGIBLE, ELIGIBILITY_UNKNOWN}


def test_open_solicitation_no_special_vehicle():
    gate = evaluate_eligibility_gate(
        {
            "title": "Commercial cotter pins NSN 5315-00-123-4567",
            "description": "Open competition. Full and open. Unrestricted.",
        },
        profile={"vehicles_held": [], "source": "test"},
    )
    assert gate["overall_status"] == ELIGIBILITY_NOT_APPLICABLE
    assert gate["actionable_for_quote_or_bid"] is True


def test_jcp_required_missing():
    gate = evaluate_eligibility_gate(
        {"title": "Drawing package", "description": "JCP certification DD2345 required for TDP access."},
        profile={"vehicles_held": [], "jcp_status": "UNKNOWN", "source": "test"},
    )
    assert gate["jcp_required"] is True
    assert gate["actionable_for_quote_or_bid"] is False


def test_jcp_confirmed():
    gate = evaluate_eligibility_gate(
        {"title": "Drawing package", "description": "JCP / DD2345 required for export-controlled drawings."},
        profile={"vehicles_held": [], "jcp_status": "HELD", "source": "test"},
    )
    assert gate["jcp_required"] is True
    assert gate["jcp_status"] == STATUS_HELD
    assert gate["actionable_for_quote_or_bid"] is True


def test_approved_source_unknown_blocks():
    gate = evaluate_eligibility_gate(
        {"title": "Valve", "description": "Approved sources only. QPL listed manufacturers."},
        profile={"vehicles_held": [], "approved_sources": [], "source": "test"},
    )
    assert gate["approved_source_required"] is True
    assert gate["actionable_for_quote_or_bid"] is False


def test_set_aside_not_eligible():
    gate = evaluate_eligibility_gate(
        {"title": "Widgets", "description": "Open buy", "set_aside": "WOSB"},
        profile={"vehicles_held": [], "source": "test"},
    )
    assert gate["set_aside_status"] == "MISSING"
    assert gate["actionable_for_quote_or_bid"] is False


def test_set_aside_total_sb_ok():
    gate = evaluate_eligibility_gate(
        {"title": "Widgets", "description": "Open", "set_aside": "Total Small Business"},
        profile={"vehicles_held": [], "source": "test"},
    )
    assert gate["set_aside_eligibility"] == "ELIGIBLE"


def test_vehicle_in_description_not_title():
    gate = evaluate_eligibility_gate(
        {
            "title": "Power distribution panel",
            "description": "Restricted to current BOA holders under the Army BOAST program. RFOP.",
        },
        profile={"vehicles_held": [], "source": "test"},
    )
    assert gate["boa_required"] is True or any(
        v.get("code") == "BOAST_BOA" for v in gate.get("vehicles_detected") or []
    )
    assert gate["actionable_for_quote_or_bid"] is False


def test_eligibility_outranks_quote_economics_in_phase_h_readiness():
    """P0: BOAST economics alone must not produce READY_FOR_QUOTE_OUTREACH."""
    gate = evaluate_eligibility_gate(
        {"title": BOAST_TITLE, "description": BOAST_DESC},
        profile={"vehicles_held": [], "source": "test"},
    )
    packet = {
        "eligibility_gate": gate,
        "supplier_cost_known": False,
        "phase_h_max_supplier_cost": {"maximum_allowable_supplier_cost": 85992.82},
    }
    state = _phase_h_readiness(
        identity="STRONG_MATCH",
        docs_reviewed=True,
        history_class="MODERATE_HISTORY",
        economics_state="ECONOMICS_PROMISING_QUOTE_REQUIRED",
        funding_state="FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN",
        deadline_blocked=False,
        hard_blockers=[],
        packet=packet,
    )
    assert state == STATE_ELIGIBILITY_ACTION
    assert state != STATE_QUOTE_OUTREACH


def test_eligibility_confirmed_allows_quote_path():
    gate = evaluate_eligibility_gate(
        {"title": BOAST_TITLE, "description": BOAST_DESC},
        profile={"vehicles_held": ["BOAST_BOA"], "source": "test"},
    )
    packet = {
        "eligibility_gate": gate,
        "supplier_cost_known": False,
        "phase_h_max_supplier_cost": {"maximum_allowable_supplier_cost": 85992.82},
    }
    state = _phase_h_readiness(
        identity="STRONG_MATCH",
        docs_reviewed=True,
        history_class="MODERATE_HISTORY",
        economics_state="ECONOMICS_PROMISING_QUOTE_REQUIRED",
        funding_state="FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN",
        deadline_blocked=False,
        hard_blockers=[],
        packet=packet,
    )
    assert state == STATE_QUOTE_OUTREACH


def test_pending_vehicle_is_conditional_not_quote_ready():
    gate = evaluate_eligibility_gate(
        {"title": BOAST_TITLE, "description": BOAST_DESC},
        profile={"vehicles_held": [], "vehicles_pending": ["BOAST_BOA"], "source": "test"},
    )
    assert gate["overall_status"] == ELIGIBLE_CONDITIONAL
    assert gate["actionable_for_quote_or_bid"] is False


def test_default_profile_has_no_invented_vehicles():
    p = load_company_eligibility_profile()
    assert "BOAST_BOA" not in [str(x).upper() for x in (p.get("vehicles_held") or [])]
