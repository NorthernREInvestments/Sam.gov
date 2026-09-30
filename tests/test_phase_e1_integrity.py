"""Phase E.1 — integrity repair regression + positive readiness tests."""

from __future__ import annotations

from pathlib import Path

from execution_requirements.enrichment import enrich_deal_for_operator, owner_ready_banner_allowed
from execution_requirements.profile import build_execution_compliance_profile
from execution_requirements.readiness import evaluate_owner_approval_gate, is_explicitly_true
from execution_requirements.supplier_state import (
    COMMERCIAL_SOURCE_FOUND,
    QUOTE_EXECUTABLE,
    resolve_supplier_execution_state,
)
from m3_mobile_read_model import deal_room_summary, opportunity_card_summary


def _positive_row(**extra):
    base = {
        "canonical_id": "pos-ready",
        "title": "Executable commercial",
        "description": (
            "RFQ POS Part Number: ABC-99 brand name or equal Quantity: 10 EA "
            "FOB DESTINATION Delivery within 30 days Commercial packaging acceptable "
            "Submit via email to buy@example.invalid"
        ),
        "owner_readiness_fixture_confirm_all": True,
        "supplier_validated": True,
        "quote_executable": True,
        "formal_quote": True,
        "preferred_supplier": "Validated Wholesale LLC",
        "supplier_quote": {"quoted_total": 5000, "source": "SUPPLIER_FORMAL"},
        "acquisition_cost": 400,
        "supported_profit": 12000,
        "government_revenue": 20000,
        "economics_complete": True,
        "financing_compatible": True,
        "funding_state": "COMPATIBLE",
        "owner_cash_required_before_payment": 0,
        "personal_guarantee_required": False,
        "submission_complete": True,
        "packaging_resolved": True,
        "delivery_feasible": True,
        "amendments_resolved": True,
        "product_identity_confirmed": True,
        "quantity_uom_confirmed": True,
    }
    base.update(extra)
    return base


# --- FALSE READINESS ---


def test_fr001_economics_unknown_blocks():
    """FR-001: economics UNKNOWN + supplier/financing resolved → NOT owner ready."""
    row = _positive_row(
        economics_complete=None,
        supported_profit="UNKNOWN",
        acquisition_cost="UNKNOWN",
        government_revenue="UNKNOWN",
    )
    # remove fixture confirm-all economics path
    del row["economics_complete"]
    profile = build_execution_compliance_profile(row, text=row["description"])
    assert profile["ready_for_owner_approval"] is False
    assert "ECONOMICS_INCOMPLETE_OR_UNKNOWN" in profile["execution_critical_blockers"]


def test_fr002_financing_unknown_blocks():
    """FR-002: financing UNKNOWN + economics/supplier ok → NOT owner ready."""
    row = _positive_row()
    del row["financing_compatible"]
    row["funding_state"] = "UNKNOWN"
    profile = build_execution_compliance_profile(row, text=row["description"])
    assert profile["ready_for_owner_approval"] is False
    assert "FINANCING_INCOMPATIBLE_OR_UNKNOWN" in profile["execution_critical_blockers"]


def test_fr003_public_price_not_executable_quote():
    """FR-003: public commercial price without formal quote → NOT owner ready."""
    row = {
        "canonical_id": "fr003",
        "description": "RFQ Part Number: X Quantity: 5 EA FOB DESTINATION Submit via email",
        "current_public_price": 199.99,
        "preferred_supplier": "Amazon-like seller",
        "economics_complete": True,
        "supported_profit": 1000,
        "acquisition_cost": 100,  # still need validated+executable quote
        "financing_compatible": True,
        "funding_state": "COMPATIBLE",
        "owner_cash_required_before_payment": 0,
        "submission_complete": True,
        "packaging_resolved": True,
        "delivery_feasible": True,
        "amendments_resolved": True,
        "product_identity_confirmed": True,
        "quantity_uom_confirmed": True,
        "owner_readiness_fixture_confirm_all": True,
    }
    st = resolve_supplier_execution_state(row)
    assert st["state"] in {COMMERCIAL_SOURCE_FOUND, "SUPPLIER_CANDIDATE"}
    assert st["supplier_validated"] is not True or st["quote_executable"] is not True
    profile = build_execution_compliance_profile(row, text=row["description"])
    assert profile["ready_for_owner_approval"] is False


def test_fr004_bid_preparation_does_not_override_blocked_gate():
    """FR-004: operator BID_PREPARATION + blocked gate → UI must not show READY."""
    row = {
        "canonical_id": "fr004",
        "description": "RFQ Quantity: 1 EA Part Number: Z",
        "operator_workflow_state": "BID_PREPARATION",
        "Deal_state": "READY",
        "lifecycle": "READY",
    }
    enriched = enrich_deal_for_operator(row, text=row["description"])
    assert enriched["ready_for_owner_approval"] is False
    assert owner_ready_banner_allowed(enriched) is False
    # Mirror UI rule: only gate truth
    assert not (
        enriched.get("operator_workflow_state") == "BID_PREPARATION"
        and enriched.get("ready_for_owner_approval") is True
    )


def test_fr005_dashboard_and_deep_dive_agree_on_blocker():
    """FR-005: dashboard card and deal-room must both show not ready when blocker exists."""
    row = {
        "canonical_id": "fr005",
        "title": "Blocked deal",
        "description": "RFQ Quantity: 3 EA Part Number: BLK FOB DESTINATION Delivery 30 days",
        "operator_workflow_state": "BID_PREPARATION",
    }
    card = opportunity_card_summary(row)
    deal = deal_room_summary(row)
    assert card.get("ready_for_owner_approval") is False
    assert deal.get("ready_for_owner_approval") is False
    assert owner_ready_banner_allowed(card) is False
    assert owner_ready_banner_allowed(deal) is False
    # Both surfaces expose blockers
    assert card.get("execution_critical_blockers") or (card.get("owner_approval_gate") or {}).get("blockers")
    assert deal.get("execution_critical_blockers") or (deal.get("owner_approval_gate") or {}).get("blockers")


def test_fr006_commercial_seller_without_validation():
    """FR-006: commercial seller exists, no supplier validation → unresolved for owner gate."""
    row = {
        "canonical_id": "fr006",
        "preferred_supplier": "Retail Seller Inc",
        "current_public_price": 49.0,
        "description": "RFQ Part X Quantity: 2 EA",
    }
    st = resolve_supplier_execution_state(row)
    assert st["supplier_validated"] is not True
    assert st["state"] != QUOTE_EXECUTABLE
    gate = evaluate_owner_approval_gate(requirements=[], row=row)
    assert gate["ready_for_owner_approval"] is False
    assert "SUPPLIER_NOT_VALIDATED" in gate["blockers"] or "QUOTE_NOT_EXECUTABLE" in gate["blockers"]


# --- POSITIVE READINESS ---


def test_pr001_simple_commercial_ready():
    """PR-001 simple commercial — owner gate true + surfaces consistent."""
    row = _positive_row(canonical_id="pr001")
    profile = build_execution_compliance_profile(row, text=row["description"])
    assert profile["ready_for_owner_approval"] is True
    card = opportunity_card_summary(row)
    deal = deal_room_summary(row)
    assert card["ready_for_owner_approval"] is True
    assert deal["ready_for_owner_approval"] is True
    assert owner_ready_banner_allowed(deal) is True
    assert (deal.get("owner_approval_gate") or {}).get("ready_for_owner_approval") is True


def test_pr002_dibbs_approved_source_ready():
    """PR-002 DIBBS approved source path."""
    row = _positive_row(
        canonical_id="pr002",
        description=(
            "DIBBS RFQ NSN: 5310-00-123-4567 Approved source CAGE 12345 "
            "Quantity: 100 EA FOB ORIGIN MIL-STD-2073 packaging Destination acceptance Submit via DIBBS"
        ),
        preferred_supplier="Approved Source Cage 12345",
        approved_source_resolved=True,
    )
    enriched = enrich_deal_for_operator(row, text=row["description"])
    assert enriched["ready_for_owner_approval"] is True
    deal = deal_room_summary(row)
    assert deal["ready_for_owner_approval"] is True


def test_pr003_minor_incidental_still_ready():
    """PR-003 product with minor incidental CoC — still owner ready when confirmed."""
    row = _positive_row(
        canonical_id="pr003",
        description=(
            "RFQ HP EliteBook brand name or equal Quantity: 12 EA FOB DESTINATION "
            "Certificate of Conformance required. Commercial packaging ASTM D3951. Submit via email."
        ),
    )
    profile = build_execution_compliance_profile(row, text=row["description"])
    assert profile["ready_for_owner_approval"] is True


def test_unknown_never_passes_is_explicitly_true():
    assert is_explicitly_true(True) is True
    assert is_explicitly_true(False) is False
    assert is_explicitly_true(None) is False
    assert is_explicitly_true("UNKNOWN") is False
    assert is_explicitly_true(0) is False


def test_ui_js_no_longer_overrides_gate_with_bid_prep():
    """Static check: READY banner must not OR BID_PREPARATION / allPass."""
    js = Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js"
    text = js.read_text(encoding="utf-8")
    assert "gateReady || allPass || APPROVAL_STATES.has(ow)" not in text
    assert 'SUPPLIER_VALIDATION: "Supplier Contacted"' not in text
    assert 'FINANCE_REVIEW: "Quote Received"' not in text
    assert 'BID_PREPARATION: "Ready For Approval"' not in text
    assert 'BID_PREPARATION: "Bid Preparation"' in text
    assert "READY FOR OWNER APPROVAL" in text


def test_ui_role_copy_is_view_mode_not_rbac():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert "View as Owner" in html
    assert "View as Operator" in html
    assert "not authentication or access control" in html.lower() or "not access control" in html


def test_positive_golden_corpus_cases():
    from validation_harness.seed_corpus import main as seed
    from validation_harness.runner import run_case

    seed()
    for cid in ("CASE_019", "CASE_020", "CASE_021"):
        r = run_case(cid)
        assert r["ready_for_owner_approval"] is True, (cid, r.get("gaps"), r.get("actuals", {}).get("owner_readiness"))
        assert r["status"] in {"PASSED", "PARTIAL"}


def test_broader_pipeline_cases_depth():
    from validation_harness.seed_corpus import main as seed
    from validation_harness.runner import run_corpus

    seed()
    run = run_corpus(validation_depth="broader_pipeline")
    assert run["depth_breakdown"]["broader_pipeline_golden_cases"] >= 5
    assert run["failed"] == 0 or run["passed"] + run["partial"] >= 4
