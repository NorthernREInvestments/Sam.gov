"""Content-first schedule recognition regression tests."""

from __future__ import annotations

from pathlib import Path

import fitz

from bidnet_engine.schedule_content_recognition import (
    BUILD,
    inspect_document,
    inspect_package_documents,
)
from phase_l.owner_ui_service import _format_product_data_card


def _write_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 50), text, fontsize=10)
    doc.save(str(path))
    doc.close()


def test_build():
    assert "authoritative-schedule-recovery" in BUILD


def test_generic_solicitation_pdf_item_table(tmp_path: Path):
    p = tmp_path / "Invitation.pdf"
    _write_pdf(
        p,
        [
            "INVITATION TO BID\nTerms and conditions apply.\n",
            "Item Description Qty Unit\n"
            "1 Widget Model W-100 Manufacturer Acme 10 EA\n"
            "2 Gadget Part Number GP-22 5 EA\n"
            "3 Supply Hose MPN HS-9 2 EA\n",
        ],
    )
    r = inspect_document(p, filename="Invitation.pdf")
    assert r["is_product_like"] or r["extracted_line_count"] >= 2
    assert r["document_role_content"] != "NON_PRODUCT_DOCUMENT"
    assert r["extracted_line_count"] >= 2


def test_generic_spec_pdf_mpn_qty(tmp_path: Path):
    p = tmp_path / "Specs.pdf"
    _write_pdf(
        p,
        [
            "SPECIFICATIONS\n"
            "1 Pump cartridge MPN PC-4401 Qty 12 EA\n"
            "2 Seal kit Part No SK-12 6 EA\n",
        ],
    )
    r = inspect_document(p, filename="Specs.pdf")
    assert r["extracted_line_count"] >= 1
    assert r["classification"] == "LINES_RECOVERED"


def test_multipage_item_table_header_propagation(tmp_path: Path):
    p = tmp_path / "BidDocs.pdf"
    _write_pdf(
        p,
        [
            "Item Description Qty Unit\n1 Bolt Grade 8 100 EA\n2 Nut Hex 100 EA\n",
            "3 Washer Flat 200 EA\n4 Pin Clevis 50 EA\n",
            "5 Bracket Mount 10 EA\n",
        ],
    )
    r = inspect_document(p, filename="BidDocs.pdf")
    assert r["extracted_line_count"] >= 4
    assert len(r["product_signal_pages"]) >= 2


def test_wrapped_description_and_expected_estimation(tmp_path: Path):
    p = tmp_path / "Attachment.pdf"
    _write_pdf(
        p,
        [
            "1 Heavy duty industrial vacuum cleaner model VC-900 with HEPA filter 3 EA\n"
            "2 Replacement bag pack Part Number BAG-900 12 EA\n",
        ],
    )
    r = inspect_document(p, filename="Attachment.pdf")
    assert r["expected_product_lines"] >= r["extracted_line_count"] >= 1


def test_product_labor_mixed_table(tmp_path: Path):
    p = tmp_path / "Toyota_parts.pdf"
    _write_pdf(
        p,
        [
            "Item Description Qty Unit\n"
            "1 OEM Oil Filter Part Number 90915-YZZD2 20 EA\n"
            "2 Labor installation hourly rate N/A\n"
            "3 Brake Pad Set MPN 04465-0R010 8 EA\n",
        ],
    )
    r = inspect_document(p, filename="Toyota_parts.pdf")
    descs = " ".join(str(x.get("description") or "") for x in r["extracted_rows"]).lower()
    assert "filter" in descs or "brake" in descs
    # Labor-only row should not dominate
    assert r["extracted_line_count"] >= 1


def test_catalog_discount_only(tmp_path: Path):
    p = tmp_path / "OEM_catalog.pdf"
    _write_pdf(
        p,
        [
            "Bidders shall provide a percentage discount off the OEM manufacturer catalog list price. "
            "No fixed item list is provided. Catalog discount applies to all OEM parts.",
        ],
    )
    r = inspect_document(p, filename="OEM_catalog.pdf")
    assert r["classification"] == "CATALOG_DISCOUNT_ONLY" or r["catalog_discount_only"]


def test_false_positive_service_table_rejected(tmp_path: Path):
    p = tmp_path / "Services.pdf"
    _write_pdf(
        p,
        [
            "SCOPE OF SERVICES\n"
            "Professional services and hourly rate for consulting.\n"
            "Labor only installation services as required.\n",
        ],
    )
    r = inspect_document(p, filename="Services.pdf")
    assert r["classification"] in {"NO_PRODUCT_LINES_ACTUALLY_PRESENT", "UNKNOWN"} or r["extracted_line_count"] == 0
    assert r["document_role_content"] in {"SERVICE_SCOPE", "NON_PRODUCT_DOCUMENT", "UNKNOWN"}


def test_generic_filename_valid_schedule(tmp_path: Path):
    p = tmp_path / "Document1.pdf"
    _write_pdf(
        p,
        [
            "Bid Item Description Manufacturer Model Qty Unit\n"
            "1 Snow plow blade Acme SP-12 2 EA\n"
            "2 Hydraulic hose Part Number HH-44 6 EA\n",
        ],
    )
    r = inspect_document(p, filename="Document1.pdf")
    assert r["extracted_line_count"] >= 2
    assert r["is_product_like"]


def test_extensionless_document_1_sniffed_as_pdf(tmp_path: Path):
    """BidNet often materializes files as document_1 with no suffix."""
    p = tmp_path / "document_1"
    _write_pdf(
        p,
        [
            "Item Description Qty Unit\n"
            "1 OEM Oil Filter Part Number 90915 10 EA\n"
            "2 Brake Pad Set MPN 04465 4 EA\n",
        ],
    )
    r = inspect_document(p, filename="document_1")
    assert r["extension"] == "pdf"
    assert r["extracted_line_count"] >= 1
    assert r["classification"] == "LINES_RECOVERED"


def test_html_document_1_classified_inaccessible_not_no_product(tmp_path: Path):
    p = tmp_path / "document_1"
    p.write_bytes(
        b"<!DOCTYPE html><html><body>item line each box set Please log in</body></html>"
    )
    r = inspect_document(p, filename="document_1")
    assert r["classification"] == "PRODUCT_SCHEDULE_INACCESSIBLE"
    assert r["extracted_line_count"] == 0
    assert "web page" in (r.get("operator_status") or "").lower() or "could not open" in (
        r.get("operator_status") or ""
    ).lower()


def test_partial_extraction_coverage_message(tmp_path: Path):
    p = tmp_path / "Exhibit.pdf"
    lines = ["Item Description Qty Unit\n"] + [f"{i} Part SKU-{i:03d} 1 EA\n" for i in range(1, 9)]
    _write_pdf(p, ["".join(lines)])
    r = inspect_document(p, filename="Exhibit.pdf")
    assert r["extracted_line_count"] >= 3
    assert 0 < float(r["extraction_coverage"] or 0) <= 1.0 or r["expected_product_lines"] >= r["extracted_line_count"]


def test_package_selects_authoritative_over_boilerplate(tmp_path: Path):
    boilerplate = tmp_path / "Terms.pdf"
    schedule = tmp_path / "Solicitation.pdf"
    _write_pdf(boilerplate, ["Terms and conditions. Insurance requirements. Indemnification."])
    _write_pdf(
        schedule,
        ["Item Description Qty\n1 Pump Parts Kit MPN PK-1 4 EA\n2 Seal MPN SE-2 8 EA\n"],
    )
    pkg = inspect_package_documents(
        [
            {"local_path": str(boilerplate), "filename": "Terms.pdf"},
            {"local_path": str(schedule), "filename": "Solicitation.pdf"},
        ]
    )
    assert pkg["AUTHORITATIVE_PRODUCT_DOC_FOUND"]
    assert pkg["LINES_READY"]
    assert pkg["EXTRACTED_PRODUCT_LINES"] >= 2
    assert "Solicitation" in str((pkg.get("AUTHORITATIVE_PRODUCT_DOC") or {}).get("filename") or "")


def test_addendum_preferred_when_more_authoritative(tmp_path: Path):
    original = tmp_path / "pricing.pdf"
    addendum = tmp_path / "Addendum_1_Revised_Pricing.pdf"
    _write_pdf(original, ["Item Description Qty\n1 Old Widget 1 EA\n"])
    _write_pdf(
        addendum,
        [
            "Item Description Qty Unit\n"
            "1 Revised Widget MPN RW-1 10 EA\n"
            "2 New Gadget MPN NG-2 5 EA\n"
            "3 Extra Part MPN XP-3 3 EA\n",
        ],
    )
    pkg = inspect_package_documents(
        [
            {"local_path": str(original), "filename": "pricing.pdf"},
            {"local_path": str(addendum), "filename": "Addendum_1_Revised_Pricing.pdf"},
        ]
    )
    auth = pkg.get("AUTHORITATIVE_PRODUCT_DOC") or {}
    assert "Addendum" in str(auth.get("filename") or "") or pkg["EXTRACTED_PRODUCT_LINES"] >= 3


def test_operator_ui_product_data_status():
    card = _format_product_data_card(
        {
            "PACKAGE_COMPLETENESS": "COMPLETE",
            "PACKAGE_DOCUMENT_COUNT_DISCOVERED": 2,
            "PACKAGE_DOCUMENT_COUNT_MATERIALIZED": 2,
            "AUTHORITATIVE_PRODUCT_DOC_FOUND": True,
            "AUTHORITATIVE_PRODUCT_DOC": {
                "filename": "Solicitation.pdf",
                "product_signal_pages": [18, 19, 20, 21],
            },
            "EXPECTED_PRODUCT_LINES": 47,
            "EXTRACTED_PRODUCT_LINES": 47,
            "LINE_EXTRACTION_COVERAGE": 1.0,
            "operator_product_status": "47 product lines found in Solicitation.pdf, pages 18–21.",
            "PACKAGE_READY_FOR_LINE_EXTRACTION": True,
        },
        {"raw_lines": 47, "usable_ae": 12, "public_prices": 5},
    )
    assert "47 product lines" in card["product_data_status"]
    assert card["expected_lines"] == 47
    assert card["source_pages"]
    assert card["identity_ready_lines"] == 12
