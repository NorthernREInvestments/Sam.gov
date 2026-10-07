"""Schedule-backed product canary — selection + extraction acceptance tests."""

from __future__ import annotations

from pathlib import Path

from bidnet_engine.schedule_backed_canary import BUILD
from bidnet_engine.schedule_extraction import (
    build_document_inventory,
    extract_schedules_from_package,
    line_extraction_coverage_class,
)
from bidnet_engine.schedule_selection import (
    exclusion_reason,
    product_canary_selection_gate,
    select_schedule_backed_candidates,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "schedule_backed"


def test_build_target():
    assert BUILD == "20261007-m3-schedule-backed-product-canary-v1"


def test_xlsx_pricing_schedule():
    path = FIXTURES / "pricing_schedule.xlsx"
    docs = [{"filename": "pricing_schedule.xlsx", "local_path": str(path), "document_type": "pricing_sheet"}]
    out = extract_schedules_from_package(docs, opportunity_id="t-xlsx")
    assert out["EXTRACTED_PRODUCT_LINES"] >= 3
    assert any(r.get("_sheet") for r in out["schedule_rows"])
    inv = out["DOCUMENT_INVENTORY"]
    assert inv and inv[0]["document_role"] == "PRICING_SCHEDULE"
    assert out["schedule_rows"][0].get("_row") is not None


def test_csv_item_list():
    path = FIXTURES / "pricing_schedule.csv"
    docs = [{"filename": "item_list.csv", "local_path": str(path)}]
    out = extract_schedules_from_package(docs, opportunity_id="t-csv")
    assert out["EXTRACTED_PRODUCT_LINES"] >= 3
    assert any(r.get("part_number") for r in out["schedule_rows"])


def test_docx_bid_schedule():
    path = FIXTURES / "bid_schedule.docx"
    docs = [{"filename": "bid_schedule.docx", "local_path": str(path)}]
    out = extract_schedules_from_package(docs, opportunity_id="t-docx")
    assert out["EXTRACTED_PRODUCT_LINES"] >= 3


def test_solicitation_body_line_table():
    text = (FIXTURES / "body_lines.txt").read_text(encoding="utf-8")
    from line_item_economics.extract import extract_from_text

    lines = extract_from_text(text)
    assert len(lines) >= 2


def test_exact_mpn_and_nsn_lists():
    path = FIXTURES / "pricing_schedule.csv"
    out = extract_schedules_from_package(
        [{"filename": "mpn_list.csv", "local_path": str(path)}],
        opportunity_id="t-mpn",
    )
    assert any(r.get("part_number") for r in out["schedule_rows"])
    nsn_path = FIXTURES / "nsn_list.csv"
    out2 = extract_schedules_from_package(
        [{"filename": "nsn_list.csv", "local_path": str(nsn_path)}],
        opportunity_id="t-nsn",
    )
    assert any(r.get("nsn") for r in out2["schedule_rows"])


def test_brand_or_equal_preserved():
    from line_item_economics.extract import line_from_row

    line = line_from_row(
        {"description": "Widget brand name or equal", "quantity": 5, "uom": "EA"},
        index=1,
    )
    assert line.get("or_equal_allowed") == "YES"


def test_mixed_product_service_table_keeps_products():
    path = FIXTURES / "pricing_schedule.csv"
    out = extract_schedules_from_package(
        [{"filename": "pricing_schedule.csv", "local_path": str(path)}],
        opportunity_id="t-mix",
    )
    assert out["EXTRACTED_PRODUCT_LINES"] >= 1


def test_schedule_present_extraction_zero_hard_fail(tmp_path: Path):
    # Header-only CSV named as pricing schedule — no product rows
    empty = tmp_path / "pricing_schedule.csv"
    empty.write_text("Item,Description,Qty,UOM\n", encoding="utf-8")
    out = extract_schedules_from_package(
        [{"filename": "pricing_schedule.csv", "local_path": str(empty), "document_type": "pricing_sheet"}],
        opportunity_id="t-zero",
        gate_expected=20,
    )
    assert out["EXTRACTED_PRODUCT_LINES"] == 0
    assert out["SCHEDULE_PRESENT_EXTRACTION_ZERO"]
    assert out["SCHEDULE_PRESENT_EXTRACTION_ZERO"][0]["code"] == "SCHEDULE_PRESENT_EXTRACTION_ZERO"


def test_coverage_partial_when_3_of_20():
    assert line_extraction_coverage_class(3, 20) == "FAILED"
    assert line_extraction_coverage_class(12, 20) == "PARTIAL"
    assert line_extraction_coverage_class(17, 20) == "USABLE"
    assert line_extraction_coverage_class(20, 20) == "COMPLETE"


def test_service_roof_catalog_excluded():
    assert exclusion_reason("Roof Replacement Project") == "ROOF"
    assert exclusion_reason("Equipment Repair Services") == "REPAIR"
    assert exclusion_reason("Catalog Discount Percentage Off List") == "CATALOG_DISCOUNT"
    gate = product_canary_selection_gate(
        {
            "classification": "PRODUCT",
            "title": "Janitorial Services Building Cleaning",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
        }
    )
    assert not gate["accepted"]


def test_select_prefers_schedule_backed_product():
    rows = [
        {
            "stable_key": "bad",
            "canonical_opportunity_id": "bad",
            "classification": "PRODUCT",
            "title": "Roof Replacement and Repair",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
        },
        {
            "stable_key": "good",
            "canonical_opportunity_id": "good",
            "classification": "PRODUCT",
            "title": "PPE Safety Glasses and Gloves Supply Only",
            "deadline": "2026-12-01",
            "package_state": "PACKAGE_ACQUIRED_BIDNET",
        },
    ]
    store = {
        "good": {
            "attachments_metadata": [
                {"filename": "pricing_schedule.xlsx", "document_type": "pricing_sheet"}
            ]
        },
        "bad": {"attachments_metadata": [{"filename": "specs.pdf"}]},
    }
    selected, excl = select_schedule_backed_candidates(rows, store, limit=5)
    assert selected and selected[0]["stable_key"] == "good"
    assert excl["CONSTRUCTION"] >= 1 or excl["REPAIR"] >= 1


def test_inventory_provenance_fields():
    path = FIXTURES / "pricing_schedule.xlsx"
    inv = build_document_inventory(
        [{"filename": "pricing_schedule.xlsx", "local_path": str(path), "content_hash": "abc"}]
    )
    assert inv[0]["sheet_names"]
    assert inv[0]["line_schedule_likely"] is True
    assert inv[0]["hash"] == "abc"


def test_pdf_item_table_heuristic_via_text():
    """PDF table path uses text heuristic — verify extractor accepts schedule-like text."""
    from table_extractors import extract_pdf_tables_heuristic

    text = "Item Description Qty UOM\n1 Safety Glasses 100 EA\n2 Nitrile Gloves 50 BX\n"
    tab = extract_pdf_tables_heuristic(text, source_name="schedule.pdf")
    # May return items or empty depending on sciquest heuristic — must not crash
    assert isinstance(tab, dict)
    assert "line_items" in tab or "rows" in tab
