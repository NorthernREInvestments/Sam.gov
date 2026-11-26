"""Corrective-build regressions: source_documents, completeness, readiness, signed URLs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.document_inventory import (
    PKG_PARTIAL_DOCUMENTS,
    PKG_SUBSTANTIVE_BODY,
    STATUS_DOWNLOAD_PENDING,
    build_inventory_from_list_documents,
    extract_document_entries,
    package_completeness,
    select_best_source_document,
    signed_url_expired,
)


def test_source_documents_parsed_from_nextbid_shape():
    payload = {
        "ok": True,
        "source_documents": [
            {
                "name": "Bulk_Propane_Fuel_Supply.pdf",
                "bytes": 382631,
                "sha256": "996c49135176d8e5583103be087aaf6ee4ed0d072423b7b2399d41e48326bb4c",
                "source_url": "https://example.invalid/x.pdf",
                "download": "/v1/opportunities/NB1/documents/996c.../download",
                "stored": True,
            }
        ],
        "nextbid_documents": [],
        "originals_available": 1,
    }
    entries = extract_document_entries(payload)
    assert len(entries) == 1
    assert entries[0]["_list_key"] == "source_documents"
    assert entries[0]["sha256"].startswith("996c")
    # Wrong keys alone must not be required
    assert extract_document_entries({"documents": [], "files": [], "items": [], "results": []}) == []


def test_inventory_metadata_not_treated_as_contents():
    inv = build_inventory_from_list_documents(
        "NB_TEST",
        {
            "source_documents": [
                {"name": "Spec.pdf", "sha256": "abc123", "bytes": 1000, "download": "/x"}
            ]
        },
    )
    assert inv["documents"][0]["is_inventory_metadata_only"] is True
    assert inv["documents"][0]["is_document_content"] is False
    assert inv["documents"][0]["acquisition_status"] in {STATUS_DOWNLOAD_PENDING, "DISCOVERED", "DOWNLOAD_PENDING"}


def test_select_best_document_avoids_specimen_contract():
    docs = [
        {"name": "Specimen_Contract_-_Professional_Services.pdf", "bytes": 500000, "sha256": "aaa"},
        {"name": "Bulk_Propane_Fuel_Supply.pdf", "bytes": 380000, "sha256": "bbb"},
    ]
    best = select_best_source_document(docs, prefer_name_substrings=["propane"])
    assert best is not None
    assert "Propane" in best["name"]


def test_signed_url_expired():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert signed_url_expired({"download": {"expires_at": past}}) is True
    assert signed_url_expired({"download": {"expires_at": future}}) is False
    assert signed_url_expired({"download": {}}) is None


def test_package_completeness_ignores_long_text():
    # Long local text + reported attachments not downloaded → partial, not complete
    result = package_completeness(
        opportunity_document_count=9,
        inventory={
            "documents": [
                {"acquisition_status": STATUS_DOWNLOAD_PENDING, "name": "a.pdf"},
                {"acquisition_status": STATUS_DOWNLOAD_PENDING, "name": "b.pdf"},
            ]
        },
        evidence_package={
            "coverage": {"has_substantive_document_content": True, "listing_preview_count": 0}
        },
    )
    assert result["package_completeness"] == PKG_PARTIAL_DOCUMENTS
    assert result["text_length_not_used_as_completeness_proof"] is True


def test_body_only_zero_docs_is_substantive_not_complete_reviewed():
    result = package_completeness(
        opportunity_document_count=0,
        inventory={"documents": []},
        evidence_package={"coverage": {"has_substantive_document_content": True, "listing_preview_count": 0}},
        required_docs_reviewed=False,
    )
    assert result["package_completeness"] == PKG_SUBSTANTIVE_BODY
    assert result["complete_solicitation_reviewed"] is False


def test_unknown_quantity_blocks_full_supplier_quote_ready():
    corpus = (
        "2PEAK GLOBAL LIFETIME ANTIFREEZE AND COOLANT #PXA0B1 5 DRUM(S) "
        "ENGINE OIL, 10W-30, FULL SYNTHETIC MOTOR OIL MUST MEET CRITERIA"
    )
    evidence = {
        "nb_id": "NB_FAKE",
        "title": "Lubricants",
        "assembled_text": corpus,
        "coverage": {
            "has_substantive_document_content": True,
            "substantive_doc_count": 1,
            "total_content_chars": len(corpus),
            "package_completeness": "substantive_solicitation_body",
            "document_inventory": {"downloaded": 0, "download_pending": 0},
            "opportunity_document_count": 0,
        },
        "document_texts": [
            {
                "doc_id": "t",
                "kind": "full_text_store",
                "text": corpus,
                "chars": len(corpus),
            }
        ],
        "inventory": {
            "deadline_at": "2026-12-31T12:00:00+00:00",
            "response_deadline": "2026-12-31T12:00:00+00:00",
            "solicitation_status": "open",
        },
    }
    analysis = {
        "is_product_purchase": True,
        "requirement_class": "product_supply",
        "line_items": [
            {
                "description": "2PEAK GLOBAL LIFETIME ANTIFREEZE AND COOLANT #PXA0B1",
                "quantity": "5",
                "unit": "DRUM(S)",
                "source_snippet": "2PEAK GLOBAL LIFETIME ANTIFREEZE",
            },
            {
                "description": "ENGINE OIL, 10W-30, FULL SYNTHETIC MOTOR OIL",
                "quantity": "UNKNOWN",
                "unit": None,
                "source_snippet": "ENGINE OIL, 10W-30, FULL SYNTHETIC MOTOR OIL",
            },
        ],
        "deadlines": [],
        "hidden_obligations": [],
    }
    verified = verify_analysis_against_sources(analysis, evidence, item=dict(evidence["inventory"]))
    assert verified["promotion"]["supplier_quote_ready"] is False
    assert verified["promotion"]["bid_submission_ready"] is False
    assert "unknown_mandatory_line_quantities" in verified["promotion"]["readiness_blockers"]
    assert any(x.startswith("quantity:") for x in verified["unknown_fields"])
    # Partial may be true when some lines have qty
    assert verified["promotion"].get("partial_supplier_quote") is True
