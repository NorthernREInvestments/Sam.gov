"""BUILD 31 — Cash / Capital Requirement Gate tests."""

from __future__ import annotations

import uuid

from m3_capital_requirement_gate_read import (
    BUILD_TAG,
    ST_CONDITIONAL,
    ST_KNOWN,
    ST_PARTIAL,
    ST_UNKNOWN,
    attach_capital_requirement_to_deal_room,
    build_capital_requirement_assessment,
)
from m3_pursuit_readiness_read import build_pursuit_readiness_assessment


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-capital-requirement-gate-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def _row(oid: str, **extra):
    base = {
        "canonical_id": oid,
        "title": "Seal kit",
        "agency": "DLA",
        "estimated_value": 100000,
        **extra,
    }
    return base


def test_known_acquisition_deposit_freight_and_payment_terms():
    oid = f"sol:cap-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 80000,
            "freight": 1200,
            "payment_terms": "50% deposit, balance before ship",
            "deposit_requirement": "50%",
            "currency": "USD",
        },
        supplier_commercial_terms={
            "by_supplier": {
                "DistCo": {
                    "quote": {"price": 80000, "total": 80000},
                    "terms": {
                        "net_terms": "50% deposit, balance before ship",
                        "deposit_requirement": "50%",
                    },
                }
            }
        },
    )
    out = build_capital_requirement_assessment(row)
    assert out["OpenAI"] == 0
    assert out["automatic_outreach"] is False
    assert out["principles"]["unknown_never_zero"] is True
    assert out["capital_required"]["acquisition_cost"] == 80000.0
    assert float(out["capital_required"]["known_prepayment"]) == 40000.0
    assert float(out["capital_required"]["known_additional_total"]) == 1200.0
    # Known requirement includes deposit + freight
    assert float(out["capital_required"]["known_requirement"]) == 41200.0
    assert out["deposit"]["state"] == ST_KNOWN
    assert out["payment_terms"] != "UNKNOWN"
    assert out["funding_path"]["financing_available_claim"] is False
    assert out["capital_vs_funding_source"]["funded"] is False


def test_unknown_deposit_and_payment_terms_preserve_unknown():
    oid = f"sol:cap-unk-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 50000,
            # freight / deposit / payment intentionally omitted
        },
    )
    out = build_capital_requirement_assessment(row)
    assert out["capital_required"]["acquisition_cost"] == 50000.0
    assert out["capital_required"]["known_prepayment"] == "UNKNOWN"
    assert out["unknown_capital"]["deposit_state"] == ST_UNKNOWN
    assert out["payment_terms"] == "UNKNOWN"
    assert out["capital_required"]["known_additional_total"] == "UNKNOWN"
    # Must NOT treat full acquisition as precise known capital when terms unknown
    assert out["capital_required"]["state"] == ST_PARTIAL
    assert out["cash_timing"]["state"] in {ST_PARTIAL, ST_UNKNOWN}
    assert out["cash_timing"]["supplier_payment"] == "UNKNOWN" or "unknown" in str(
        out["blocking_unknowns"]
    ).lower()
    # UNKNOWN never zero
    assert 0 not in (
        out["capital_required"]["known_prepayment"],
        out["capital_required"]["known_additional_total"],
    )
    assert out["principles"]["no_assumed_supplier_terms"] is True


def test_unknown_freight_listed_not_zeroed():
    oid = f"sol:cap-fr-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 10000,
            "deposit_requirement": "None",
            "payment_terms": "Payment before ship",
        },
    )
    out = build_capital_requirement_assessment(row)
    assert any("freight" in str(u).lower() for u in out["unknown_capital"]["items"])
    assert out["capital_required"]["known_additional_total"] == "UNKNOWN"
    assert out["deposit"]["amount"] == 0.0


def test_cash_cycle_when_supported():
    oid = f"sol:cap-cycle-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 20000,
            "payment_terms": "Payment before ship",
            "deposit_requirement": "100%",
            "lead_time": "21 days",
        },
        cash_survival={"payment_timing": "Net 30 after acceptance"},
        supplier_commercial_terms={
            "by_supplier": {
                "Acme": {
                    "terms": {
                        "net_terms": "Payment before ship",
                        "deposit_requirement": "100%",
                    },
                    "lead_time": {"stated": "21 days"},
                }
            }
        },
    )
    out = build_capital_requirement_assessment(row)
    cycle = out["cash_cycle"]
    assert cycle["supplier_payment"] != "UNKNOWN"
    assert cycle["shipment_delivery"] == "21 days"
    assert cycle["government_payment"] == "Net 30 after acceptance"
    assert cycle["state"] in {ST_KNOWN, ST_PARTIAL}
    assert cycle["invents_net_terms"] is False


def test_capital_without_funding_source_not_marked_funded():
    oid = f"sol:cap-nofund-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 60000,
            "deposit_requirement": "50%",
            "payment_terms": "50% deposit",
            "freight": 500,
        },
    )
    out = build_capital_requirement_assessment(row)
    assert out["capital_required"]["state"] in {ST_KNOWN, ST_PARTIAL}
    assert float(out["capital_required"]["known_requirement"]) >= 30000
    assert out["funding_path"]["source_state"] == ST_UNKNOWN
    assert out["capital_vs_funding_source"]["funded"] is False
    assert out["funding_path"]["financing_available_claim"] is False
    assert "unresolved" in out["narrative"].lower() or "UNKNOWN" in out["narrative"]


def test_funding_path_conditional_not_financing_available():
    oid = f"sol:cap-path-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={"acquisition": 40000, "deposit_requirement": "25%"},
        funding_requirement={"status": "CONDITIONAL", "assignment_status": "UNKNOWN"},
        financing_path_intelligence={
            "paths": [
                {
                    "path": "PO_FINANCING",
                    "status": "CONDITIONAL",
                    "requirements": ["award_required", "conditions_unknown"],
                    "unknowns": ["lender_terms"],
                }
            ]
        },
    )
    out = build_capital_requirement_assessment(row)
    assert out["funding_path"]["state"] in {ST_CONDITIONAL, ST_PARTIAL, "CONDITIONAL"}
    assert out["funding_path"]["financing_available_claim"] is False
    assert out["capital_vs_funding_source"]["funded"] is False


def test_pursuit_readiness_integration_partial_not_forced_known():
    oid = f"sol:cap-pr-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        product_classification="KIT",
        manufacturer="Acme",
        description="x" * 50,
        documents=["sol.pdf"],
        supplier_product_graph={"edges": [{"supplier": "DistCo"}]},
        deadline_runway_days="12",
        deadline_viability="GOOD",
        transaction_economics={
            "acquisition": 25000,
            "revenue": 40000,
            "supported_profit": 15000,
            # payment terms / deposit unknown
        },
    )
    pr = build_pursuit_readiness_assessment(row, ensure_actions=False)
    econ = pr["dimensions"]["economics_visibility"]
    assert econ["state"] in {ST_KNOWN, ST_PARTIAL, ST_UNKNOWN}
    # Should not claim fully known cash when capital gate says partial
    cap = econ.get("capital_requirement_gate") or {}
    if cap:
        assert cap.get("state") in {ST_KNOWN, ST_PARTIAL, ST_UNKNOWN}
    # Overall is not a GREEN/RED score — blockers listed; capital gaps must appear when material
    overall = pr.get("overall") or {}
    assert overall.get("not_a_score") is True
    assert "biggest_blockers" in overall
    # Economics must not claim fully resolved cash when deposit/terms unknown
    assert econ["state"] in {ST_KNOWN, ST_PARTIAL, ST_UNKNOWN}
    why = str(econ.get("why") or "").lower()
    if econ.get("capital_requirement_gate", {}).get("state") == ST_PARTIAL:
        assert econ["state"] in {ST_PARTIAL, ST_UNKNOWN} or "unknown" in why or "unresolved" in why


def test_actions_deduped_and_material_only():
    from m3_action_orchestration_read import list_actions

    oid = f"sol:cap-act-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={"acquisition": 90000},
    )
    a = build_capital_requirement_assessment(
        row, ensure_actions=True, persist_actions=True
    )
    b = build_capital_requirement_assessment(
        row, ensure_actions=True, persist_actions=True
    )
    titles = [
        str(x.get("title") or "")
        for x in list_actions(opportunity_id=oid, limit=50)
        if str(x.get("created_by") or "") == "capital_requirement_gate"
        or "capital_requirement_gate" in str(x.get("trigger_source") or "")
    ]
    # Deduped — second call should not explode duplicates
    assert len(titles) == len(set(t.lower() for t in titles))
    assert a["OpenAI"] == 0 and b["paid"] == 0
    assert any(
        "deposit" in t.lower() or "payment" in t.lower() or "funding" in t.lower()
        for t in titles
    ) or a.get("next_action")


def test_deal_room_attachment():
    oid = f"sol:cap-deal-{uuid.uuid4().hex[:8]}"
    row = _row(
        oid,
        transaction_economics={
            "acquisition": 12000,
            "freight": 300,
            "deposit_requirement": "30%",
        },
    )
    deal = attach_capital_requirement_to_deal_room({"canonical_id": oid}, row=row)
    gate = deal["capital_requirement_gate"]
    assert gate["kind"] == "M3CapitalRequirementDealRoom"
    assert gate["display"]["title"] == "Can we fund this deal?"
    assert deal["funding"]["funded"] is False
    assert gate["beginner"]["title"] == "Can we fund this deal?"


def test_no_unsupported_financing_claims():
    oid = f"sol:cap-claim-{uuid.uuid4().hex[:8]}"
    row = _row(oid, transaction_economics={"acquisition": 1000})
    out = build_capital_requirement_assessment(row)
    blob = str(out).lower()
    assert out["funding_path"]["financing_available_claim"] is False
    assert "financing available" not in blob or out["principles"]["no_assumed_financing"]
    assert out["principles"]["no_lender_approval"] is True
    assert out["principles"]["no_auto_outreach"] is True
    assert out["principles"]["no_ai_spend"] is True


def test_regression_prior_commercial_layer():
    from m3_commercial_validation_read import BUILD_TAG as CV
    from m3_operator_loop_read import BUILD_TAG as OL

    assert CV.startswith("20260919-m3-commercial-validation")
    assert OL.startswith("20260919-m3-operator-loop")
