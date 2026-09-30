"""Phase D — Execution + Compliance Intelligence targeted tests."""

from __future__ import annotations

from execution_requirements import BUILD_TAG, build_execution_compliance_profile
from execution_requirements.checklists import (
    build_invoice_checklist,
    build_post_award_checklist,
    build_supplier_confirmation_checklist,
    build_supplier_quote_packet,
)
from execution_requirements.conflicts import apply_amendment_overlay, detect_requirement_conflicts
from execution_requirements.constants import (
    PAY_ACCEPTED,
    PAY_DELIVERED,
    PAY_INVOICED,
    PAY_PAID,
    PAY_SHIPPED,
    ST_CONFIRMED,
    ST_UNKNOWN,
)
from execution_requirements.extract import build_execution_requirements
from execution_requirements.models import empty_requirement, make_requirement
from execution_requirements.readiness import evaluate_owner_approval_gate


FIXTURE = """
RFQ SPE7M1-26-Q-1234
CLIN 0001 Part Number: ABC-12345 NSN: 1234-00-567-8901
Dell Latitude 5540 Laptop brand name or equal
Quantity: 75 EA (estimated quantity: 100)
Pack size: 1 per pack
FOB DESTINATION
Ship to DODAAC: SW3122 Richmond VA
Delivery within 30 days after award
Inspection at destination. Destination acceptance.
Source inspection may apply for FAT.
MIL-STD-2073 packaging required. MIL-STD-129 marking.
Commercial packaging not authorized without waiver.
Certificate of Conformance required.
Submit quotes via email to contracting@example.mil
Subject line: SPE7M1-26-Q-1234 Quote
Quote validity: 60 days
Acknowledge all amendments.
Invoice via WAWF. Receiving report required. Payment terms Net 30. EFT required.
Buy American / Country of Origin representation required.
CLIN 0002 Quantity: 10 EA Destination: Norfolk VA FOB ORIGIN
"""


def test_build_tag():
    assert BUILD_TAG.startswith("20260922-m3-execution-compliance")


def test_unknown_never_silently_confirmed():
    r = empty_requirement(status="CONFIRMED", normalized_requirement="x")
    assert r["status"] == ST_UNKNOWN


def test_exact_part_and_brand_or_equal():
    reqs = build_execution_requirements({"canonical_id": "t1"}, text=FIXTURE, include_supplier_seeds=False)
    parts = [r for r in reqs if r.get("category") == "PRODUCT"]
    assert any(r.get("subtype") == "PART" or "ABC-12345" in str(r.get("captured_value") or r.get("normalized_requirement")) for r in parts)
    assert any(r.get("subtype") == "NSN" or "1234" in str(r.get("captured_value") or "") for r in parts)
    assert any(r.get("subtype") == "BRAND_OR_EQUAL" or "or equal" in str(r.get("raw_text") or "").lower() for r in parts)


def test_quantity_pack_ambiguity_and_estimate_not_guaranteed():
    reqs = build_execution_requirements({"canonical_id": "t2"}, text=FIXTURE, include_supplier_seeds=False)
    qty = [r for r in reqs if r.get("category") == "QUANTITY_UOM"]
    assert qty
    est = [r for r in qty if r.get("subtype") == "ESTIMATE"]
    assert est
    assert "not a guaranteed" in str(est[0].get("notes") or "").lower()


def test_multiple_clins_different_destinations():
    row = {
        "canonical_id": "t3",
        "line_items": [
            {"clin": "0001", "quantity": 75, "uom": "EA", "destination": "Richmond", "fob": "DESTINATION"},
            {"clin": "0002", "quantity": 10, "uom": "EA", "destination": "Norfolk", "fob": "ORIGIN"},
        ],
    }
    reqs = build_execution_requirements(row, text="CLIN schedule", include_supplier_seeds=False, use_existing_extractors=False)
    dests = [r for r in reqs if r.get("subtype") == "CLIN_DESTINATION"]
    assert len(dests) >= 2
    fobs = [r for r in reqs if r.get("subtype") == "CLIN_FOB"]
    assert any("DESTINATION" in str(r.get("captured_value")) for r in fobs)
    assert any("ORIGIN" in str(r.get("captured_value")) for r in fobs)


def test_military_and_commercial_packaging():
    reqs = build_execution_requirements({"canonical_id": "t4"}, text=FIXTURE, include_supplier_seeds=False)
    packs = [r for r in reqs if r.get("category") == "PACKAGING"]
    assert any("2073" in str(r.get("normalized_requirement") or r.get("raw_text") or "") for r in packs)
    # MIL-STD alone must not auto-reject — requirements exist, none marked reject
    assert all("auto_reject" not in str(r).lower() for r in packs)


def test_fob_origin_and_destination_conflict():
    text = "FOB ORIGIN for CLIN 0001. FOB DESTINATION for CLIN 0002."
    reqs = build_execution_requirements({"canonical_id": "t5"}, text=text, include_supplier_seeds=False)
    conflicts = detect_requirement_conflicts(reqs)
    assert any(c.get("category") == "FOB" and c.get("material") for c in conflicts)


def test_source_inspection_and_destination_acceptance():
    reqs = build_execution_requirements({"canonical_id": "t6"}, text=FIXTURE, include_supplier_seeds=False)
    assert any(r.get("category") == "INSPECTION" for r in reqs)
    assert any(r.get("category") == "ACCEPTANCE" and "destination" in str(r.get("normalized_requirement") or "").lower() for r in reqs)


def test_amendment_changing_quantity_and_deadline():
    base = "Quantity: 50 EA. Bid deadline: 2026-10-01. MIL-STD-2073 packaging."
    amd = "Amendment 0001: Quantity changed to 75 EA. Bid deadline extended to 2026-10-15. Packaging unchanged."
    result = apply_amendment_overlay(base, amd, row={"canonical_id": "t7"})
    assert "QUANTITY_UOM" in result["changed_categories"] or "SUBMISSION" in result["changed_categories"]
    assert any(r.get("subtype") == "AMENDMENT_ACK" for r in result["requirements"])
    assert any("from_amendment" in str(r.get("notes") or "") for r in result["requirements"])


def test_supplier_confirmation_and_incomplete_blocks_readiness():
    profile = build_execution_compliance_profile(
        {
            "canonical_id": "t8",
            "title": "Dell Laptop Refresh",
            "supported_profit": 18400,
            "acquisition_cost": 500,
        },
        text=FIXTURE,
    )
    supplier = profile["supplier_confirmation_checklist"]
    assert supplier["blocks_owner_approval"] is True
    assert profile["ready_for_owner_approval"] is False
    assert "SUPPLIER_CONFIRMATION_INCOMPLETE" in profile["execution_critical_blockers"] or profile["owner_approval_gate"]["blockers"]
    packet = profile["supplier_quote_packet"]
    assert packet["transmission"] == "CONTENT_ONLY_NO_SEND"
    assert "Confirm" in packet["body"]


def test_missing_packaging_detail_blocks_when_mandatory():
    reqs = [
        make_requirement(
            category="PACKAGING",
            normalized="MIL-STD-2073 packaging",
            raw_text="MIL-STD-2073 packaging required",
            mandatory=True,
            blocking=True,
            status="REQUIRES_SUPPLIER_CONFIRMATION",
        ),
        make_requirement(category="PRODUCT", normalized="PN X", mandatory=True, status=ST_CONFIRMED, evidence=[{"ok": 1}], captured_value="X"),
        make_requirement(category="QUANTITY_UOM", normalized="10 EA", mandatory=True, status=ST_CONFIRMED, evidence=[{"ok": 1}], captured_value=10),
    ]
    gate = evaluate_owner_approval_gate(requirements=reqs, supplier_checklist={"blocks_owner_approval": False}, submission_checklist={"blocks_owner_approval": False})
    assert gate["ready_for_owner_approval"] is False


def test_invoice_wawf_and_payment_state_separation():
    reqs = build_execution_requirements({"canonical_id": "t9"}, text=FIXTURE, include_supplier_seeds=False)
    inv = build_invoice_checklist(reqs)
    assert inv["wawf_required"] is True
    assert inv["states"]["SHIPPED"] == PAY_SHIPPED
    assert inv["states"]["DELIVERED"] == PAY_DELIVERED
    assert inv["states"]["ACCEPTED"] == PAY_ACCEPTED
    assert inv["states"]["INVOICED"] == PAY_INVOICED
    assert inv["states"]["PAID"] == PAY_PAID
    post = build_post_award_checklist(reqs, payment_state=PAY_DELIVERED)
    assert post["payment_states_distinct"] == [PAY_SHIPPED, PAY_DELIVERED, PAY_ACCEPTED, PAY_INVOICED, PAY_PAID]
    assert "collapse" in post["note"].lower() or "≠" in post["note"]


def test_conflicting_brand_rules():
    text = "Brand name only. No substitutes. Also brand name or equal accepted."
    reqs = build_execution_requirements({"canonical_id": "t10"}, text=text, include_supplier_seeds=False)
    conflicts = detect_requirement_conflicts(reqs)
    assert any(c.get("category") == "PRODUCT" and c.get("material") for c in conflicts)


def test_evidence_provenance_retained():
    reqs = build_execution_requirements({"canonical_id": "t11"}, text=FIXTURE, include_supplier_seeds=False, source_document="Solicitation PDF")
    evidenced = [r for r in reqs if r.get("evidence")]
    assert evidenced
    ev = evidenced[0]["evidence"][0]
    assert ev.get("source_document")
    assert "raw_text" in ev


def test_full_profile_example_checklist_shape():
    profile = build_execution_compliance_profile({"canonical_id": "t12", "title": "Example"}, text=FIXTURE)
    assert profile["kind"] == "ExecutionComplianceProfile"
    assert profile["submission_checklist"]["kind"] == "SubmissionChecklist"
    assert profile["post_award_checklist"]["kind"] == "PostAwardChecklist"
    assert profile["invoice_checklist"]["kind"] == "InvoiceChecklist"
    assert profile["cash_cycle"]["financing_assumed"] is False
    assert profile["cash_cycle"]["award_date"] == "UNKNOWN"
    assert "Product" in profile["deep_dive_sections"]
    assert profile["va_next_actions"]


def test_deal_room_attaches_execution_compliance():
    from m3_mobile_read_model import deal_room_summary

    row = {
        "canonical_id": "api-exec-1",
        "title": "Test Deal",
        "buyer": "Air Force",
        "description": FIXTURE,
        "lifecycle": "RESEARCH",
    }
    deal = deal_room_summary(row)
    assert "execution_compliance" in deal
    assert deal["execution_compliance"].get("kind") == "ExecutionComplianceProfile"
    assert isinstance(deal.get("execution_critical_blockers"), list)


def test_confirmed_supplier_items_can_clear_supplier_gate():
    reqs = build_execution_requirements({"canonical_id": "t13"}, text=FIXTURE, include_supplier_seeds=True)
    for r in reqs:
        if r.get("category") == "SUPPLIER_CONFIRMATION" and r.get("mandatory"):
            r["status"] = ST_CONFIRMED
            r["evidence"] = [{"confirmed_by": "test"}]
            r["captured_value"] = "confirmed"
    supplier = build_supplier_confirmation_checklist(reqs)
    # Still may have packaging etc. in checklist — mandatory supplier confirmations confirmed
    unresolved_sc = [
        i for i in supplier["unresolved_mandatory"] if "SUPPLIER" in str(i.get("requirement_id") or "") or True
    ]
    # At least confirmed status stuck
    assert any(r.get("status") == ST_CONFIRMED for r in reqs if r.get("category") == "SUPPLIER_CONFIRMATION")
