"""Seed Phase E golden corpus — SYNTHETIC_VALIDATION_FIXTURE cases only."""

from __future__ import annotations

import json
from pathlib import Path

from validation_harness.models import SOURCE_TYPE_SYNTHETIC, empty_case

OUT = Path(__file__).resolve().parent / "corpus"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []

    def add(**kw):
        c = empty_case(**kw)
        c["source_type"] = SOURCE_TYPE_SYNTHETIC
        cases.append(c)

    add(
        case_id="CASE_001",
        case_name="Simple commercial product RFQ",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\n"
            "RFQ CITY-IT-001\nAgency: Fixture City Procurement\n"
            "Purchase of Dell Latitude 5540 laptop brand name or equal\n"
            "Quantity: 50 EA\nFOB DESTINATION\nDelivery within 30 days after award\n"
            "Submit quotes via email to buying@example.invalid\n"
            "Subject line: CITY-IT-001 Quote\n"
        ),
        expected={
            "product_identity": {"brand_or_equal_detected": True},
            "quantity_uom": {"has_quantity": True},
            "shipping_delivery": {"fob_destination": True, "delivery_aro_detected": True},
            "financing": {"financing_assumed": False},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["commercial", "product", "delivery", "baseline"],
        notes="SYNTHETIC_VALIDATION_FIXTURE",
    )
    add(
        case_id="CASE_002",
        case_name="Exact manufacturer/part-number requirement",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ SPE7M1-26-Q-EXACT\n"
            "Part Number: ABC-12345 NSN: 1234-00-567-8901\n"
            "Manufacturer: AcmeCorp Model: X100\nBrand name only. No substitutes.\nQuantity: 25 EA\n"
        ),
        expected={
            "product_identity": {
                "part_number_detected": True,
                "nsn_detected": True,
                "brand_only_detected": True,
                "brand_or_equal_detected": False,
            },
            "quantity_uom": {"has_quantity": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["identity", "product", "exact_part"],
    )
    add(
        case_id="CASE_003",
        case_name="Brand-or-equal",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ BOE-003\n"
            "HP EliteBook 840 brand name or equal\nQuantity: 12 EA\nFOB DESTINATION\n"
        ),
        expected={
            "product_identity": {"brand_or_equal_detected": True, "brand_only_detected": False},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["identity", "brand_or_equal"],
    )
    add(
        case_id="CASE_004",
        case_name="DIBBS approved-source NSN",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nDIBBS RFQ SPE7M1-26-Q-NSN\n"
            "NSN: 5310-00-123-4567\nApproved source CAGE 12345 required.\n"
            "Quantity: 100 EA\nFOB ORIGIN\nInspection at destination. Destination acceptance.\n"
        ),
        expected={
            "product_identity": {"nsn_detected": True},
            "shipping_delivery": {"fob_origin": True},
            "inspection_acceptance": {"destination_acceptance": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["dibbs", "nsn", "identity", "inspection"],
    )
    add(
        case_id="CASE_005",
        case_name="MIL-STD-2073 packaging",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ PACK-2073\nPart Number: PKG-55 Quantity: 40 EA\n"
            "MIL-STD-2073 packaging required. MIL-STD-129 marking.\n"
            "Commercial packaging not authorized without waiver.\n"
        ),
        expected={
            "packaging": {"mil_std_2073_detected": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["packaging", "mil_std"],
    )
    add(
        case_id="CASE_006",
        case_name="UOM/pack ambiguity",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ UOM-006\n"
            "Quantity: 10 HD (hundred). Pack size: 12 per pack.\n"
            "Estimated quantity: 500\nPart Number: UOM-1\n"
        ),
        expected={
            "quantity_uom": {"has_quantity": True, "estimate_flagged_not_guaranteed": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["quantity", "uom", "pack"],
    )
    add(
        case_id="CASE_007",
        case_name="Multiple CLINs and destinations",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ MULTI-CLIN-007\n"
            "CLIN 0001 Qty 100 EA Destination Richmond FOB DESTINATION Delivery 30 days ARO\n"
            "CLIN 0002 Qty 250 EA Destination Norfolk FOB ORIGIN Delivery 45 days ARO\n"
            "Part Number: MC-100\n"
        ),
        row_overrides={
            "line_items": [
                {"clin": "0001", "quantity": 100, "uom": "EA", "destination": "Richmond", "fob": "DESTINATION"},
                {"clin": "0002", "quantity": 250, "uom": "EA", "destination": "Norfolk", "fob": "ORIGIN"},
            ]
        },
        expected={
            "quantity_uom": {"has_quantity": True, "multi_clin_destinations": 2},
            "shipping_delivery": {"fob_destination": True, "fob_origin": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["clin", "delivery", "quantity", "multi_clin"],
    )
    add(
        case_id="CASE_008",
        case_name="IDIQ estimate vs minimum order",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nIDIQ BPA-008\n"
            "Estimated annual quantity: 40823\nMinimum order: 2267 EA\n"
            "Part Number: IDIQ-77\nDo not treat estimated annual quantity as guaranteed purchase.\n"
        ),
        expected={
            "quantity_uom": {"estimate_flagged_not_guaranteed": True, "has_quantity": True},
            "financing": {"financing_assumed": False},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["idiq", "quantity", "financing"],
        notes="estimate != order quantity; ceiling != guaranteed revenue",
    )
    add(
        case_id="CASE_009",
        case_name="First article requirement",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ FAT-009\nPart Number: FAT-1 Quantity: 15 EA\n"
            "First Article Test (FAT) required prior to production.\n"
            "Source inspection may apply for FAT.\nDestination acceptance after approval.\n"
        ),
        expected={
            "inspection_acceptance": {
                "first_article": True,
                "source_inspection": True,
                "destination_acceptance": True,
            },
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["inspection", "first_article", "fat"],
    )
    add(
        case_id="CASE_010",
        case_name="Source inspection",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ SRC-INSP-010\nPart Number: SI-2 Quantity: 8 EA\n"
            "Source inspection required at contractor facility.\nCertificate of Conformance required.\n"
        ),
        expected={
            "inspection_acceptance": {"source_inspection": True, "certificate_of_conformance": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["inspection", "acceptance"],
    )
    add(
        case_id="CASE_011",
        case_name="Amendment changes quantity and deadline",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ AMD-011\nQuantity: 100 EA\n"
            "Bid deadline: 2026-10-10\nPart Number: AMD-9\nFOB DESTINATION\nAcknowledge all amendments.\n"
        ),
        amendment_text=(
            "SYNTHETIC_VALIDATION_FIXTURE Amendment 0001\nQuantity changed to 150 EA.\n"
            "Bid deadline extended to 2026-10-14.\nFOB DESTINATION unchanged.\nAcknowledge amendment.\n"
        ),
        expected={
            "amendment": {"amendment_applied": True},
            "submission": {"amendment_ack_required": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["amendment", "quantity", "submission"],
    )
    add(
        case_id="CASE_012",
        case_name="WAWF and receiving report",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ INV-012\nPart Number: INV-3 Quantity: 6 EA\n"
            "FOB DESTINATION Destination acceptance.\n"
            "Invoice via WAWF. Receiving report required. Payment terms Net 30. EFT required.\n"
        ),
        expected={
            "invoice_payment": {"wawf_required": True},
            "inspection_acceptance": {"destination_acceptance": True},
            "post_award": {"states_note_distinct": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["wawf", "invoice", "payment", "post_award"],
    )
    add(
        case_id="CASE_013",
        case_name="Supplier unknown blocks readiness",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ SUP-013 Quantity: 10 EA Part Number: S-1 "
            "FOB DESTINATION Submit via email.\n"
        ),
        row_overrides={"supplier_unknown": True, "Deal_state": "READY_FOR_OPERATOR_ACTION"},
        expected={
            "supplier": {"supplier_confirmation_incomplete": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        expected_blockers=["SUPPLIER"],
        tags=["adversarial", "supplier", "false_readiness", "readiness"],
        adversarial=True,
    )
    add(
        case_id="CASE_014",
        case_name="Financing incompatible under owner constraints",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ FIN-014 Quantity: 20 EA Part Number: F-2 Payment Net 30.\n"
        ),
        row_overrides={
            "funding_state": "UNRESOLVED_PG_REQUIRED",
            "personal_guarantee_required": True,
            "Deal_state": "COMPLETE",
        },
        expected={
            "financing": {"financing_assumed": False},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["adversarial", "financing", "false_readiness"],
        adversarial=True,
    )
    add(
        case_id="CASE_015",
        case_name="Mandatory submission requirements present and unresolved",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ SUB-015\nPart Number: SUB-4 Quantity: 3 EA\n"
            "Submit via email to contracting@example.invalid\nSubject line: SUB-015 Quote\n"
            "Quote validity: 30 days\nAcknowledge all amendments.\n"
            "Signed representations and certifications required.\n"
        ),
        expected={
            "submission": {"has_submission_requirements": True, "amendment_ack_required": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["submission", "adversarial", "false_readiness"],
        adversarial=True,
    )
    add(
        case_id="CASE_016",
        case_name="Expired solicitation",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ EXP-016 Quantity: 4 EA Part Number: E-1\n"
            "Bid deadline: 2020-01-01\nSubmit via email.\n"
        ),
        row_overrides={"deadline": "2020-01-01", "days_remaining": -400, "Deal_state": "READY"},
        expected={"owner_readiness": {"ready_for_owner_approval": False}},
        expected_owner_readiness=False,
        tags=["submission", "deadline", "adversarial", "false_readiness"],
        adversarial=True,
    )
    add(
        case_id="CASE_017",
        case_name="Commercial ASTM packaging",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ ASTM-017 Quantity: 9 EA Part Number: A-1\n"
            "Commercial packaging ASTM D3951 acceptable.\n"
        ),
        expected={
            "packaging": {"commercial_packaging_detected": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["packaging", "commercial"],
    )
    add(
        case_id="CASE_018",
        case_name="Specialist packaging house",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ SPI-018 Quantity: 7 EA Part Number: P-9\n"
            "MIL-STD-2073 packaging required. Specialist packaging house may be required. SPI required.\n"
        ),
        expected={
            "packaging": {"mil_std_2073_detected": True, "specialist_packaging_flagged": True},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["packaging", "specialist", "false_rejection"],
        validation_depth="execution_path",
    )

    # --- Phase E.1 positive READY cases ---
    _POS_READY = {
        "owner_readiness_fixture_confirm_all": True,
        "supplier_validated": True,
        "quote_executable": True,
        "formal_quote": True,
        "preferred_supplier": "Validated Wholesale LLC",
        "supplier_quote": {"quoted_total": 22500, "source": "SUPPLIER_FORMAL", "notes": "formal quote"},
        "acquisition_cost": 450,
        "supported_profit": 18400,
        "government_revenue": 52000,
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
    add(
        case_id="CASE_019",
        case_name="POSITIVE A — Simple commercial product executable",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ POS-A-019\nAgency: Fixture City Procurement\n"
            "Purchase of Dell Latitude 5540 laptop brand name or equal\n"
            "Quantity: 50 EA\nFOB DESTINATION\nDelivery within 30 days after award\n"
            "Commercial packaging acceptable.\n"
            "Submit quotes via email to buying@example.invalid\n"
            "Subject line: POS-A-019 Quote\n"
        ),
        row_overrides=dict(_POS_READY),
        expected={
            "product_identity": {"brand_or_equal_detected": True},
            "quantity_uom": {"has_quantity": True},
            "shipping_delivery": {"fob_destination": True},
            "owner_readiness": {"ready_for_owner_approval": True},
            "supplier": {"supplier_validated": True, "quote_executable": True},
            "economics": {"economics_complete": True},
            "financing": {"financing_compatible": True, "financing_assumed": False},
        },
        expected_owner_readiness=True,
        tags=["positive_ready", "commercial", "product"],
        validation_depth="positive_ready",
        notes="POSITIVE CASE A — all mandatory conditions explicitly satisfied",
    )
    add(
        case_id="CASE_020",
        case_name="POSITIVE B — DIBBS approved source executable",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nDIBBS RFQ POS-B-020\n"
            "NSN: 5310-00-123-4567\nApproved source CAGE 12345 required.\n"
            "Quantity: 100 EA\nFOB ORIGIN\nInspection at destination. Destination acceptance.\n"
            "MIL-STD-2073 packaging required.\nSubmit via DIBBS.\n"
        ),
        row_overrides={
            **_POS_READY,
            "preferred_supplier": "Approved Source Cage 12345",
            "approved_source_resolved": True,
        },
        expected={
            "product_identity": {"nsn_detected": True},
            "shipping_delivery": {"fob_origin": True},
            "inspection_acceptance": {"destination_acceptance": True},
            "packaging": {"mil_std_2073_detected": True},
            "owner_readiness": {"ready_for_owner_approval": True},
            "supplier": {"supplier_validated": True, "quote_executable": True},
        },
        expected_owner_readiness=True,
        tags=["positive_ready", "dibbs", "nsn"],
        validation_depth="positive_ready",
        notes="POSITIVE CASE B — DIBBS NSN with approved source path resolved",
    )
    add(
        case_id="CASE_021",
        case_name="POSITIVE C — Commercial with minor incidental requirement",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ POS-C-021\n"
            "HP EliteBook 840 brand name or equal\nQuantity: 12 EA\nFOB DESTINATION\n"
            "Delivery within 45 days after award\n"
            "Certificate of Conformance required (incidental).\n"
            "Commercial packaging ASTM D3951 acceptable.\n"
            "Submit quotes via email to buying@example.invalid\n"
        ),
        row_overrides=dict(_POS_READY),
        expected={
            "product_identity": {"brand_or_equal_detected": True},
            "packaging": {"commercial_packaging_detected": True},
            "owner_readiness": {"ready_for_owner_approval": True},
            "supplier": {"supplier_validated": True, "quote_executable": True},
        },
        expected_owner_readiness=True,
        tags=["positive_ready", "commercial", "incidental"],
        validation_depth="positive_ready",
        notes="POSITIVE CASE C — minor incidental CoC must not over-reject when fixture confirms execution",
    )

    # --- Broader-pipeline golden cases (frozen fixtures; more expected truth) ---
    add(
        case_id="BP_001",
        case_name="Broader pipeline — commercial incomplete (blocked)",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ BP-001\nAgency: Fixture Agency\n"
            "Dell Latitude 5540 brand name or equal\nQuantity: 20 EA\nFOB DESTINATION\n"
            "Delivery within 30 days\nCommercial packaging acceptable\n"
            "Submit via email to buy@example.invalid\n"
        ),
        row_overrides={"current_public_price": 899.0, "product_classification": "IT_HARDWARE"},
        expected={
            "classification": {"product_classification": "IT_HARDWARE"},
            "product_identity": {"brand_or_equal_detected": True},
            "quantity_uom": {"has_quantity": True},
            "supplier": {"state": "COMMERCIAL_SOURCE_FOUND", "supplier_validated": False},
            "economics": {"economics_complete": False},
            "financing": {"financing_compatible": False},
            "owner_readiness": {"ready_for_owner_approval": False},
            "execution_blockers_include_any": ["SUPPLIER_NOT_VALIDATED", "QUOTE_NOT_EXECUTABLE", "ECONOMICS_INCOMPLETE_OR_UNKNOWN", "FINANCING_INCOMPATIBLE_OR_UNKNOWN"],
        },
        expected_owner_readiness=False,
        tags=["broader_pipeline", "commercial", "negative"],
        validation_depth="broader_pipeline",
        notes="Public price alone must not validate supplier",
    )
    add(
        case_id="BP_002",
        case_name="Broader pipeline — DIBBS incomplete (blocked)",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nDIBBS RFQ BP-002\nNSN: 5310-00-999-0001\n"
            "Approved source required\nQuantity: 50 EA\nFOB ORIGIN\n"
            "MIL-STD-2073 packaging\nDestination acceptance\n"
        ),
        row_overrides={"product_classification": "DIBBS_HARDWARE"},
        expected={
            "classification": {"product_classification": "DIBBS_HARDWARE"},
            "product_identity": {"nsn_detected": True},
            "quantity_uom": {"has_quantity": True},
            "packaging": {"mil_std_2073_detected": True},
            "supplier": {"supplier_validated": False},
            "owner_readiness": {"ready_for_owner_approval": False},
        },
        expected_owner_readiness=False,
        tags=["broader_pipeline", "dibbs", "negative"],
        validation_depth="broader_pipeline",
    )
    add(
        case_id="BP_003",
        case_name="Broader pipeline — executable commercial (ready)",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ BP-003\n"
            "Part Number: BP-COM-99 brand name or equal\nQuantity: 15 EA\nFOB DESTINATION\n"
            "Delivery within 20 days\nCommercial packaging acceptable\n"
            "Submit via email\n"
        ),
        row_overrides={
            **_POS_READY,
            "product_classification": "IT_HARDWARE",
        },
        expected={
            "classification": {"product_classification": "IT_HARDWARE"},
            "product_identity": {"brand_or_equal_detected": True},
            "quantity_uom": {"has_quantity": True},
            "supplier": {"supplier_validated": True, "quote_executable": True, "state": "QUOTE_EXECUTABLE"},
            "economics": {"economics_complete": True},
            "financing": {"financing_compatible": True},
            "owner_readiness": {"ready_for_owner_approval": True},
        },
        expected_owner_readiness=True,
        tags=["broader_pipeline", "positive_ready", "commercial"],
        validation_depth="broader_pipeline",
    )
    add(
        case_id="BP_004",
        case_name="Broader pipeline — financing UNKNOWN blocks",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ BP-004\nPart Number: FIN-1 Quantity: 8 EA\n"
            "FOB DESTINATION Delivery within 30 days\nSubmit via email\n"
        ),
        row_overrides={
            **{k: v for k, v in _POS_READY.items() if k not in {"financing_compatible", "funding_state"}},
            "financing_compatible": None,
            "funding_state": "UNKNOWN",
            "product_classification": "COMMERCIAL",
        },
        expected={
            "classification": {"product_classification": "COMMERCIAL"},
            "financing": {"financing_compatible": False},
            "owner_readiness": {"ready_for_owner_approval": False},
            "execution_blockers_include_any": ["FINANCING_INCOMPATIBLE_OR_UNKNOWN"],
        },
        expected_owner_readiness=False,
        tags=["broader_pipeline", "financing", "false_readiness"],
        validation_depth="broader_pipeline",
    )
    add(
        case_id="BP_005",
        case_name="Broader pipeline — economics UNKNOWN blocks",
        source_fixture=(
            "SYNTHETIC_VALIDATION_FIXTURE\nRFQ BP-005\nPart Number: ECON-1 Quantity: 6 EA\n"
            "FOB DESTINATION Delivery within 30 days\nSubmit via email\n"
        ),
        row_overrides={
            **{k: v for k, v in _POS_READY.items() if k not in {"economics_complete", "supported_profit", "acquisition_cost", "government_revenue"}},
            "economics_complete": None,
            "supported_profit": "UNKNOWN",
            "acquisition_cost": "UNKNOWN",
            "product_classification": "COMMERCIAL",
        },
        expected={
            "economics": {"economics_complete": False},
            "owner_readiness": {"ready_for_owner_approval": False},
            "execution_blockers_include_any": ["ECONOMICS_INCOMPLETE_OR_UNKNOWN"],
        },
        expected_owner_readiness=False,
        tags=["broader_pipeline", "economics", "false_readiness"],
        validation_depth="broader_pipeline",
    )

    for c in cases:
        if "validation_depth" not in c:
            c["validation_depth"] = "adversarial_readiness" if c.get("adversarial") else "execution_path"
        path = OUT / f"{c['case_id']}.json"
        path.write_text(json.dumps(c, indent=2), encoding="utf-8")
        print("wrote", path.name)
    print("total", len(cases))


if __name__ == "__main__":
    main()
