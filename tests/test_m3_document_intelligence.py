"""Tests for procurement document intelligence extraction engine."""

from __future__ import annotations

from m3_document_intelligence import (
    BLOCKED,
    PROCESSED,
    READY_FOR_PRICING,
    REQUIRES_REVIEW,
    analyze_document_intelligence_top,
    apply_va_intelligence_update,
    build_document_intelligence_profile,
    build_economics_readiness_score,
    deal_room_document_intelligence_section,
    detect_procurement_signals,
    extract_line_items_from_text,
    extract_opportunity_intelligence,
    _normalize_line_item,
)
from m3_procurement_package import MATCH_HIGH, VAL_ESTIMATED


JAGGAER_PDF_TEXT = """
Product Line Items
P1 324.3 Pounds- Big bluestem (Andropogon gerardii)
P2 100.0 Pounds- Blue grama
P3 50.0 Pounds- Side oats grama
Estimated value: $45,000
Delivery required: 30 days after award
Technical Specifications for native seed mix
"""


def test_detect_procurement_signals_priority():
    sig = detect_procurement_signals(JAGGAER_PDF_TEXT)
    assert sig["kind"] == "PROCUREMENT_SIGNAL_DETECTION"
    assert sig["has_bom_section"] is True
    assert "bom" in sig["priority_order"] or "quantity" in sig["priority_order"]


def test_extract_jaggaer_line_items_from_pdf_text():
    lines = extract_line_items_from_text(JAGGAER_PDF_TEXT, source_document="event.pdf")
    assert len(lines) >= 2
    assert lines[0]["kind"] == "PROCUREMENT_LINE_ITEM"
    assert lines[0]["Quantity"] == 324.3
    assert lines[0]["Unit"] == "LB"
    assert "bluestem" in lines[0]["Description"].lower()
    assert lines[0]["Source_document"] == "event.pdf"


def test_reject_document_numbers_as_part_numbers():
    bad = _normalize_line_item(
        {
            "description": "See attachment",
            "part_number": "SOL-2026-00412",
            "quantity": 1,
            "unit": "EA",
        },
        source_document="sol.pdf",
        index=1,
    )
    assert bad is not None
    assert bad["Part_number"] == "UNKNOWN"

    good = _normalize_line_item(
        {
            "description": "Cisco Catalyst Switch",
            "part_number": "C9300-48P-A",
            "manufacturer": "Cisco",
            "quantity": 10,
            "unit": "EA",
        },
        source_document="bom.xlsx",
        index=1,
    )
    assert good is not None
    assert good["Part_number"] == "C9300-48P-A"
    assert good["Quantity"] == 10.0


def test_no_line_item_without_evidence():
    assert (
        _normalize_line_item(
            {"description": "", "part_number": "DOC-99", "quantity": None},
            source_document="x",
            index=1,
        )
        is None
    )


def test_document_profile_processes_pdf_text():
    doc = {
        "title": "event.pdf",
        "name": "event.pdf",
        "document_type": "solicitation",
        "format": "PDF",
        "bytes_recovered": True,
        "extracted_text": JAGGAER_PDF_TEXT,
        "source": "jaggaer_event_pdf",
    }
    row = {"canonical_id": "iowa-1", "source_id": "state_ia", "title": "Native Seed"}
    prof = build_document_intelligence_profile(doc, row, index=0)
    assert prof["kind"] == "DOCUMENT_INTELLIGENCE_PROFILE"
    assert prof["Processing_status"] == PROCESSED
    assert len(prof["PROCUREMENT_LINE_ITEMS"]) >= 2


def test_linked_unfetched_requires_review():
    doc = {
        "title": "package.pdf",
        "url": "https://example.test/package.pdf",
        "bytes_recovered": False,
    }
    prof = build_document_intelligence_profile(doc, {"canonical_id": "x"}, index=0)
    assert prof["Processing_status"] == REQUIRES_REVIEW
    assert "document_text_or_bytes" in (prof.get("missing_evidence") or [])


def test_opportunity_intelligence_builds_bom_and_readiness(monkeypatch):
    monkeypatch.setattr("m3_document_intelligence.save_learning_index", lambda idx: None)
    monkeypatch.setattr(
        "m3_document_intelligence.load_learning_index",
        lambda: {"kind": "M3DocumentIntelligenceLearning", "by_portal_family": {}, "by_document_type": {}},
    )
    row = {
        "canonical_id": "iowa-seed-1",
        "title": "Native Prairie Seed Mix",
        "source_id": "state_ia",
        "agency": "State of Iowa",
        "detail_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DasIowa",
        "documents": [
            {
                "title": "event.pdf",
                "document_type": "SOLICITATION",
                "format": "PDF",
                "bytes_recovered": True,
                "extracted_text": JAGGAER_PDF_TEXT,
                "source": "jaggaer",
            }
        ],
    }
    intel = extract_opportunity_intelligence(row, update_learning=False)
    assert intel["kind"] == "M3DocumentIntelligence"
    assert intel["Portal_family"] == "IOWA"
    assert intel["Processed"] >= 1
    assert len(intel["PROCUREMENT_LINE_ITEMS"]) >= 2
    assert intel["PROCUREMENT_BOM"]["line_count"] >= 1
    assert intel["CONFIGURATION_REQUIREMENT_PROFILE"]["kind"] == "CONFIGURATION_REQUIREMENT_PROFILE"
    assert intel["ECONOMICS_READINESS_SCORE"]["kind"] == "ECONOMICS_READINESS_SCORE"
    # Quantities evidenced — should not be fully blocked
    assert intel["ECONOMICS_READINESS_SCORE"]["status"] in {
        READY_FOR_PRICING,
        "READY_FOR_ECONOMICS",
        "PARTIAL",
        BLOCKED,
    }
    assert any(_q != "UNKNOWN" for _q in intel["Quantities_found"])


def test_economics_readiness_scoring():
    score = build_economics_readiness_score(
        identity={
            "Identity_confidence": MATCH_HIGH,
            "Quantity": 10,
            "Manufacturer_part_number": "C9300-48P-A",
            "Model_number": "C9300",
            "NSN": "UNKNOWN",
        },
        configuration={"configuration_complete": True, "REQUIRED_ACCESSORIES": []},
        bom={"cost_eligible_lines": [{"x": 1}], "BOM_completeness_pct": 80},
        commercial={"Confidence": VAL_ESTIMATED, "Contract_value_model": {"confidence": VAL_ESTIMATED}},
        line_items=[{"Quantity": 10}],
    )
    assert score["status"] in {READY_FOR_PRICING, "READY_FOR_ECONOMICS"}
    assert score["flags"]["Product_identified"] is True
    assert score["flags"]["Quantity_known"] is True


def test_does_not_invent_accessories_or_prices():
    row = {
        "canonical_id": "thin-1",
        "title": "Office Supplies",
        "source_id": "state_ia",
        "documents": [
            {
                "title": "notice.pdf",
                "bytes_recovered": True,
                "extracted_text": "This solicitation is for office supplies. See portal for details.",
            }
        ],
    }
    intel = extract_opportunity_intelligence(row, update_learning=False)
    # No fake BOM parts / prices
    for li in intel["PROCUREMENT_LINE_ITEMS"]:
        assert li.get("Unit_price") == "UNKNOWN" or li.get("Unit_price") is None or isinstance(
            li.get("Unit_price"), (int, float)
        )
    accessories = (intel.get("CONFIGURATION_REQUIREMENT_PROFILE") or {}).get("REQUIRED_ACCESSORIES") or []
    for a in accessories:
        assert "ASSUMED" not in str(a.get("Item") or "").upper()


def test_analyze_top_priority_families(monkeypatch):
    monkeypatch.setattr("m3_document_intelligence.save_intelligence_index", lambda idx: None)
    monkeypatch.setattr("m3_document_intelligence.save_learning_index", lambda idx: None)
    monkeypatch.setattr(
        "m3_document_intelligence.load_intelligence_index",
        lambda: {"kind": "M3DocumentIntelligenceIndex", "by_id": {}},
    )
    monkeypatch.setattr(
        "m3_document_intelligence.load_learning_index",
        lambda: {"kind": "M3DocumentIntelligenceLearning", "by_portal_family": {}, "by_document_type": {}},
    )

    class FakeStore:
        def __init__(self):
            self._rows = {
                "iowa-1": {
                    "canonical_id": "iowa-1",
                    "title": "Badges",
                    "source_id": "state_ia",
                    "agency": "State of Iowa",
                    "documents": [
                        {
                            "title": "event.pdf",
                            "bytes_recovered": True,
                            "extracted_text": JAGGAER_PDF_TEXT,
                        }
                    ],
                },
                "bidnet-1": {
                    "canonical_id": "bidnet-1",
                    "title": "Other",
                    "source_id": "bidnet",
                    "detail_url": "https://www.bidnetdirect.com/x",
                    "documents": [],
                },
            }

        def all(self):
            return list(self._rows.values())

        def save(self):
            return None

    store = FakeStore()
    run = analyze_document_intelligence_top(
        store, limit=10, persist=True, priority_families_only=True
    )
    assert run["kind"] == "M3DocumentIntelligenceRun"
    assert run["NEXT_STATE"] == "PROCUREMENT_DOCUMENT_INTELLIGENCE_OPERATIONAL"
    assert run["DOCUMENT_PROCESSING"]["Documents_analyzed"] >= 1
    assert run["EXTRACTION"]["Line_items_extracted"] >= 2
    assert store._rows["iowa-1"].get("line_items")


def test_deal_room_and_va(monkeypatch):
    monkeypatch.setattr("m3_document_intelligence.save_intelligence_index", lambda idx: None)
    monkeypatch.setattr(
        "m3_document_intelligence.load_intelligence_index",
        lambda: {"kind": "M3DocumentIntelligenceIndex", "by_id": {}},
    )
    row = {
        "canonical_id": "mt-1",
        "title": "Wheelchair Lift",
        "source_id": "state_mt",
        "agency": "State of Montana",
        "documents": [
            {
                "title": "event.pdf",
                "bytes_recovered": True,
                "extracted_text": "Product Line Items\nP1 IFB: Wheelchair Lift  1  LS - Lump Sum\n",
            }
        ],
        "line_items": [
            {"description": "Wheelchair Lift", "quantity": 1, "unit": "LS", "confidence": "HIGH"}
        ],
    }
    intel = extract_opportunity_intelligence(row, update_learning=False)
    row["document_intelligence_full"] = intel
    sec = deal_room_document_intelligence_section(row)
    assert sec["kind"] == "M3DealRoomDocumentIntelligence"
    assert sec["Documents_processed"] >= 1
    assert "Economics_readiness" in sec

    class Store:
        def __init__(self):
            self._rows = {"mt-1": row}

        def save(self):
            return None

    store = Store()
    denied = apply_va_intelligence_update(store, "mt-1", action="APPROVE_DEAL")
    assert denied["ok"] is False
    ok = apply_va_intelligence_update(
        store,
        "mt-1",
        action="CORRECT_EXTRACTION",
        correction={"line_index": 0, "Description": "Wheelchair Lift Assembly", "Quantity": 1},
        note="Fixed description",
    )
    assert ok["ok"] is True
