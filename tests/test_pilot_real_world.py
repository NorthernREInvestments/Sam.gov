"""Controlled real-world pilot tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.pilot_real_world import (
    FINANCING_EXECUTION_FAIL,
    FINANCING_PATH_PLAUSIBLE,
    NEEDS_MINOR_REVIEW,
    NOT_READY,
    READY_FOR_OWNER_APPROVAL,
    VERIFIED_ACQUISITION_PRICE,
    classify_owner_readiness,
    owner_workflow_ready,
    pilot_gate,
    quote_ingestion_ready,
    screen_financing,
    verify_live_status,
)
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_D,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_B,
    SUPPLIER_D,
    VALIDATED_QUOTE_TARGET,
)
from phase_l.quote_readiness import INTERNAL_FIELDS_NEVER_SUPPLIER, build_supplier_facing_packet
from phase_l.product_page_resolution import EXACT_VERIFIED


def test_pilot_gate_rejects_gov_d_and_supplier_d():
    live = {"verified": True}
    fin = {"status": FINANCING_PATH_PLAUSIBLE}
    ok, reason = pilot_gate(
        quality_state=VALIDATED_QUOTE_TARGET,
        gov_grade=GOV_VALUE_D,
        supplier_grade=SUPPLIER_B,
        live=live,
        financing=fin,
    )
    assert ok is False and "gov_d" in reason
    ok2, reason2 = pilot_gate(
        quality_state=VALIDATED_QUOTE_TARGET,
        gov_grade=GOV_VALUE_A,
        supplier_grade=SUPPLIER_D,
        live=live,
        financing=fin,
    )
    assert ok2 is False and "supplier" in reason2


def test_pilot_gate_accepts_validated():
    ok, reason = pilot_gate(
        quality_state=VALIDATED_QUOTE_TARGET,
        gov_grade=GOV_VALUE_A,
        supplier_grade=SUPPLIER_B,
        live={"verified": True},
        financing={"status": FINANCING_PATH_PLAUSIBLE},
    )
    assert ok is True and reason == "validated"


def test_financing_fail_and_plausible():
    fail = screen_financing({"title": "X", "payment_terms": "personal guarantee required"})
    assert fail["status"] == FINANCING_EXECUTION_FAIL
    ok = screen_financing({"title": "Dell laptops"}, max_buy={"supplier_quote_target": 5000})
    assert ok["status"] == FINANCING_PATH_PLAUSIBLE
    assert ok["lenders_contacted"] is False


def test_live_status_expired():
    v = verify_live_status(
        {"agency": "City", "solicitation_id": "1", "source_url": "https://sam.gov/x"},
        original={"original_source_verified": True, "solicitation_number": "1"},
        submission={"submission_path_ready": True},
        deadline_days=-1,
    )
    assert v["verified"] is False


def test_owner_readiness_states():
    assert (
        classify_owner_readiness(
            gate_ok=True,
            quality_state=VALIDATED_QUOTE_TARGET,
            live={"verified": True},
            readiness={"ready": True},
            financing={"status": FINANCING_PATH_PLAUSIBLE},
        )
        == READY_FOR_OWNER_APPROVAL
    )
    assert (
        classify_owner_readiness(
            gate_ok=True,
            quality_state=SECONDARY_QUOTE_TARGET,
            live={"verified": True},
            readiness={"ready": False, "blockers": ["x"]},
            financing={"status": FINANCING_PATH_PLAUSIBLE},
        )
        == NEEDS_MINOR_REVIEW
    )
    assert (
        classify_owner_readiness(
            gate_ok=False,
            quality_state=VALIDATED_QUOTE_TARGET,
            live={"verified": False},
            readiness=None,
            financing={"status": FINANCING_PATH_PLAUSIBLE},
        )
        == NOT_READY
    )


def test_quote_packet_no_internal_economics():
    pkt = build_supplier_facing_packet(
        row={"title": "Ford F-150", "solicitation_id": "S1", "agency": "City"},
        commercial={"manufacturer": "Ford", "model": "F-150", "mpn": "F150"},
        original={"solicitation_number": "S1", "original_posting_url": "https://sam.gov/x"},
        uom={"quantity": 2, "uom": "EA"},
    )
    for k in INTERNAL_FIELDS_NEVER_SUPPLIER:
        assert k not in pkt
    assert "max_buy" not in pkt
    assert pkt["send_authorized"] is False


def test_quote_ingestion_and_no_verified_fabrication():
    q = quote_ingestion_ready()
    assert q["ready"] is True
    assert q["fabricated_verified"] is False
    assert VERIFIED_ACQUISITION_PRICE in q["verified_states_require_real_quote"]
    ow = owner_workflow_ready()
    assert ow["ready"] is True
    assert ow["auto_send"] is False


def test_no_caps_final_verification():
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP is True
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
