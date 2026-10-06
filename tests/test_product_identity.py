"""Regression tests for product identity — includes real-ish fixtures."""
from product_identity.classifier import classify_identity
from product_identity.line_quality import classify_extraction_row, line_quality_gate
from product_identity.models import (
    EXACT_CATALOG_NUMBER,
    EXACT_MPN,
    LINE_ITEM_CONFIRMED,
    PERMITTED_EQUAL,
    REAL_PURCHASING_LINE,
    STRONG_GENERIC_SPEC,
)
from product_identity.normalizer import ProductIdentityNormalizer
from product_identity.spreadsheet import map_headers, rows_from_matrix


def test_map_headers_and_rows():
    matrix = [
        ["Item", "Description", "Catalog #", "Model #", "Qty", "UOM"],
        ["1", "SCREW HEX FLANGE", "3944593", "3944593", "10", "EA"],
        ["Ford Meter Box"],
        ["2", "Corporation Stop", "Y502", "", "5", "EA"],
    ]
    rows = rows_from_matrix(matrix, source_path="t.xlsx", sheet="s")
    assert len(rows) >= 2
    assert rows[0]["catalog_number"] == "3944593"
    assert rows[1]["manufacturer"] == "Ford Meter Box"
    assert rows[1].get("inherited_manufacturer") is True


def test_exact_catalog_and_mpn():
    n = ProductIdentityNormalizer()
    norm = n.normalize_row(
        {"description": "TAPPET VALVE", "catalog_number": "3965966", "manufacturer": "Cummins", "quantity": 12, "uom": "EA"}
    )
    rec = classify_identity(norm)
    assert rec["identity_type"] in {EXACT_CATALOG_NUMBER, EXACT_MPN}
    assert rec["confidence_grade"] in {"A", "B", "C"}


def test_brand_or_equal():
    n = ProductIdentityNormalizer()
    norm = n.normalize_row(
        {
            "description": "Gate valve Ford Meter Box Y502 or approved equal",
            "manufacturer": "Ford Meter Box",
            "model": "Y502",
            "quantity": 2,
            "uom": "EA",
        }
    )
    rec = classify_identity(norm, extra={"equal_allowed": True})
    assert rec["identity_type"] in {PERMITTED_EQUAL, "EXACT_MODEL"}
    assert rec["equal_allowed"] is True


def test_strong_generic():
    n = ProductIdentityNormalizer()
    norm = n.normalize_row(
        {
            "description": "6 ft fiberglass step ladder, 300 lb Type IA",
            "quantity": 4,
            "uom": "EA",
        }
    )
    rec = classify_identity(norm)
    assert rec["identity_type"] == STRONG_GENERIC_SPEC
    assert rec["confidence_grade"] in {"A", "B", "C"}


def test_line_quality_gate():
    row = {
        "item": "12",
        "description": "LED 4 ft strip fixture, 5000K, 120-277V, minimum 5000 lumens",
        "quantity": 20,
        "uom": "EA",
    }
    assert classify_extraction_row(row) == REAL_PURCHASING_LINE
    q, score, _ = line_quality_gate(row, REAL_PURCHASING_LINE)
    assert q in {LINE_ITEM_CONFIRMED, "LINE_ITEM_LIKELY"}
    assert score > 0


def test_headers_mapped():
    m = map_headers(["Item #", "Product Description", "Mfr", "Part Number", "Qty"])
    assert "item" in m and "description" in m and "part_number" in m
