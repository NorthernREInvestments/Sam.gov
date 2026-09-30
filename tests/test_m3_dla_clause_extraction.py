"""BUILD 12 — Deep DLA packaging / inspection clause extraction tests."""

from __future__ import annotations

from m3_dla_clause_extraction import (
    BUILD_TAG,
    ST_UNKNOWN,
    ST_VALIDATED,
    extract_clauses_from_text,
    extract_dla_clauses,
)
from m3_offer_readiness_read import build_offer_readiness_profile


SAMPLE_GOVERNING = """
SECTION D - PACKAGING AND MARKING
D.3 Packaging shall be in accordance with MIL-STD-2073-1.
Marking shall comply with MIL-STD-129.
Preservation method: Method 33.

SECTION E - INSPECTION AND ACCEPTANCE
Inspection at origin. Acceptance at destination.
FAT (First Article Test) is required unless FAT waiver is granted.

SECTION F - DELIVERIES OR PERFORMANCE
FOB: DESTINATION
Deliver to: DLA Distribution Depot, Building 4 Warehouse
Delivery within 90 days ARO.
Shipping instructions: freight prepaid.
"""


def test_exact_clause_extraction():
    clauses = extract_clauses_from_text(
        SAMPLE_GOVERNING,
        source_document="SPE7M1-solicitation.pdf",
        document_type="SOLICITATION",
    )
    keys = {c["field_key"] for c in clauses}
    assert "mil_std_2073" in keys
    assert "mil_std_129" in keys
    assert "fob_terms" in keys
    assert "fat_required" in keys
    mil = next(c for c in clauses if c["field_key"] == "mil_std_2073")
    assert mil["status"] == ST_VALIDATED
    assert "MIL-STD-2073" in mil["evidence"]["extracted_text"].upper().replace(" ", "")
    assert mil["evidence"]["source_document"] == "SPE7M1-solicitation.pdf"
    assert mil["evidence"]["confidence"] == "HIGH"
    assert mil["evidence"]["timestamp"]
    assert mil["fabricated"] is False
    # Section hint should surface packaging section when present
    assert mil["evidence"]["section"] != "" or mil["evidence"]["section"] == "UNKNOWN"


def test_missing_documents_remain_unknown():
    row = {"canonical_id": "sol:empty:dla", "title": "Pump", "description": "NSN only"}
    bundle = extract_dla_clauses(row)
    # Short description without packaging clauses → UNKNOWN categories
    pack = bundle["category_status"]["PACKAGING"]
    assert pack["status"] == ST_UNKNOWN
    assert pack["count"] == 0
    assert "UNKNOWN" in str(pack["evidence"].get("extracted_text") or "UNKNOWN")
    assert bundle["fabricated_requirements"] is False
    assert bundle["inferred_standards"] is False


def test_false_standards_not_created():
    text = "This is a commercial RFQ for office supplies. No special handling."
    clauses = extract_clauses_from_text(text, source_document="office.pdf")
    for c in clauses:
        assert "2073" not in str(c.get("requirement") or "")
        assert "MIL-STD" not in str(c.get("value") or "").upper()
    # Explicitly: empty / unrelated text must not invent MIL-STD-2073
    assert not any(c["field_key"] == "mil_std_2073" for c in clauses)


def test_evidence_retained():
    row = {
        "canonical_id": "sol:pack1:dla",
        "documents": [
            {
                "filename": "governing.pdf",
                "document_type": "SOLICITATION",
                "page": 3,
                "extracted_text": SAMPLE_GOVERNING,
            }
        ],
    }
    bundle = extract_dla_clauses(row)
    assert bundle["clause_count"] >= 3
    for c in bundle["clauses"]:
        ev = c["evidence"]
        assert ev["source_document"]
        assert ev["extracted_text"]
        assert ev["confidence"]
        assert ev["timestamp"]
        assert "page" in ev


def test_offer_readiness_updates_with_validated_packaging():
    row = {
        "canonical_id": "sol:pack2:dla",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951",
        "product_identity": {"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        "dla_product_structure": {
            "has_exact_nsn": True,
            "packaging_signal": True,  # old signal path
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        "documents": [
            {
                "filename": "pkg.pdf",
                "extracted_text": "SECTION D\nPackaging per MIL-STD-2073-1E.\nFOB DESTINATION\n",
            }
        ],
        "deal_economics": {"PRICE_CONFIDENCE": "HIGH"},
        "execution_intelligence": {"Financing_Fit": "SATISFIED"},
    }
    profile = build_offer_readiness_profile(row, research_items=[])
    pack_rows = [
        r
        for r in profile["compliance"]["requirements"]
        if r.get("category") == "PACKAGING" and not r.get("scaffold")
    ]
    assert pack_rows
    # Evidence-backed VALIDATED should appear (not bare "Signal detected")
    validated = [r for r in pack_rows if r.get("status") == "VALIDATED" or r.get("raw_status") == "VALIDATED"]
    assert validated
    assert "MIL-STD-2073" in str(validated[0].get("evidence") or "").upper().replace(" ", "") or "2073" in str(
        validated[0].get("evidence") or ""
    )
    assert validated[0].get("source") == "dla_clause_extraction"
    assert "Signal detected" not in str(validated[0].get("evidence") or "")


def test_document_intelligence_unchanged():
    # Import still works; BUILD 12 does not replace it
    from m3_document_intelligence import extract_opportunity_intelligence, extract_line_items_from_text

    assert callable(extract_opportunity_intelligence)
    assert callable(extract_line_items_from_text)
    from discovery.dla_product_extract import extract_dla_product_structure

    struct = extract_dla_product_structure(
        {"title": "NSN 1234-01-234-5678", "description": "MIL-STD-2073 packaging"}
    )
    assert struct.get("packaging_signal") is True  # legacy signal still works


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-dla-clause-extraction")
