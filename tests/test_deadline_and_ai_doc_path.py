"""Deadline hard-rule + AI document-understanding path (offline/mocked)."""

from datetime import datetime, timedelta, timezone

from m3.research.ai_document_analysis import analyze_solicitation_documents, build_model_input_from_evidence
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.commercial_criteria import evaluate_bid_eligibility, evaluate_deadline_gate, evaluate_procurement_pipeline


def test_past_deadline_blocks_despite_inventory_open():
    item = {
        "title": "Purchase of Widgets",
        "solicitation_status": "open",
        "lifecycle_status": "open",
        "deadline_at": "2026-11-23T22:30:00.000Z",
        "verified_individual_solicitation": True,
        "record_type": "INDIVIDUAL_SOLICITATION",
        "requirement_class": "product_supply",
        "quantity": 1,
        "quantity_semantic": "ORDERED_QUANTITY",
        "document_text": "Purchase of one (1) widget. Submit bids electronically.",
    }
    now = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
    dl = evaluate_deadline_gate(item, now=now)
    assert dl["expired"] is True
    assert dl["inventory_open_ignored"] is True
    pipe = evaluate_procurement_pipeline(item, document_text=item["document_text"])
    assert pipe.get("excluded") is True
    bid = evaluate_bid_eligibility(item, pipe, document_text=item["document_text"])
    assert bid["bid_eligible"] is False
    assert "deadline_not_passed" in bid["failed_gates"] or "active_solicitation" in bid["failed_gates"]


def test_future_deadline_can_pass_deadline_gate():
    item = {
        "solicitation_status": "open",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    }
    dl = evaluate_deadline_gate(item)
    assert dl["expired"] is False
    assert dl["status"] == "pass"


def test_queue_card_deadline_field_is_honored():
    """Owner queue cards historically used `deadline` not `deadline_at`."""
    item = {
        "solicitation_status": "open",
        "deadline": "2026-11-23T22:30:00.000Z",
    }
    dl = evaluate_deadline_gate(item, now=datetime(2026, 11, 26, tzinfo=timezone.utc))
    assert dl["expired"] is True
    assert dl["inventory_open_ignored"] is True


def test_ai_path_default_does_not_call_live_openai():
    package = {
        "nb_id": "NB_TEST",
        "title": "Purchase of Test Widgets",
        "assembled_text": "x" * 900,
        "assembled_chars": 900,
        "coverage": {
            "has_substantive_document_content": True,
            "substantive_doc_count": 1,
            "total_content_chars": 900,
        },
        "document_texts": [
            {
                "doc_id": "t1",
                "kind": "full_text_store",
                "source_path": "test",
                "text": "Purchase of ten (10) test widgets. Bid due January 1, 2099. " + ("spec " * 200),
                "chars": 900,
                "is_metadata_only": False,
                "is_listing_preview": False,
            }
        ],
        "source_refs": [{"doc_id": "t1", "kind": "full_text_store", "chars": 900}],
    }
    result = analyze_solicitation_documents(None, evidence_package=package, allow_live=False)
    assert result["live_ai_invoked"] is False
    assert result.get("mocked") is False
    assert result["ok"] is False or result.get("skipped_ai")


def test_mocked_ai_is_labeled_and_verified_against_source():
    text = (
        "The agency will purchase one (1) Six Inch High Pressure Diesel Pump with a trailer. "
        "Submit bids by portal. " + ("requirements " * 100)
    )
    package = {
        "nb_id": "NB00321554",
        "title": "PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP WITH A TRAILER",
        "assembled_text": text,
        "coverage": {
            "has_substantive_document_content": True,
            "substantive_doc_count": 1,
            "total_content_chars": len(text),
        },
        "document_texts": [
            {
                "doc_id": "body",
                "kind": "full_text_store",
                "source_path": "test",
                "text": text,
                "chars": len(text),
                "is_metadata_only": False,
                "is_listing_preview": False,
            }
        ],
        "inventory": {
            "deadline_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
            "solicitation_status": "open",
        },
        "source_refs": [],
    }
    mock = {
        "requirement_class": "product_supply",
        "is_product_purchase": True,
        "line_items": [
            {
                "description": "Six Inch High Pressure Diesel Pump with a trailer",
                "quantity": "1",
                "unit": "each",
                "quantity_semantic": "ORDERED",
                "source_snippet": "purchase one (1) Six Inch High Pressure Diesel Pump",
            }
        ],
        "hidden_obligations": [],
        "missing_information": [],
        "coverage_limitations": ["mocked"],
        "confidence": "medium",
    }
    ai = analyze_solicitation_documents(None, evidence_package=package, allow_live=False, mock_response=mock)
    assert ai["mocked"] is True
    assert ai["live_ai_invoked"] is False
    verified = verify_analysis_against_sources(ai["parsed"], package)
    assert verified["verified_line_items"]
    assert verified["promotion"]["ai_analysis_alone_insufficient"] is True
    assert verified["complete_solicitation_reviewed"] is False


def test_model_input_includes_chunked_corpus_not_preview_only():
    package = {
        "nb_id": "X",
        "title": "Goods",
        "coverage": {"has_substantive_document_content": True},
        "document_texts": [
            {
                "doc_id": "long",
                "kind": "full_text_store",
                "text": ("LINE ITEM SCHEDULE qty 5 each pump. " * 800),
                "chars": 20000,
                "is_metadata_only": False,
                "is_listing_preview": False,
            }
        ],
        "source_refs": [],
    }
    mi = build_model_input_from_evidence(package)
    assert mi["chunk_count"] >= 1
    assert mi["corpus_chars"] > 500
    assert "LINE ITEM SCHEDULE" in mi["corpus"]
