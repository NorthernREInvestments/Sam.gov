"""Targeted tests for BUILD 002Q pipeline defect fixes. Offline only."""

from __future__ import annotations

from m3.research.classify import _infer_status, classify_solicitation_item
from m3.research.commercial_criteria import evaluate_bid_eligibility, evaluate_procurement_pipeline
from m3.research.document_quality import title_body_overlap
from m3.research.extract_fields import extract_quantity_fields


def test_lifecycle_status_maps_to_open():
    assert _infer_status({"lifecycle_status": "open"}) == "open"
    item = classify_solicitation_item(
        {
            "title": "Purchase of Two (2) Generators",
            "lifecycle_status": "open",
            "solicitation_status": "unknown",
            "record_type": "INDIVIDUAL_SOLICITATION",
            "verified_individual_solicitation": True,
        }
    )
    assert item["solicitation_status"] == "open"


def test_word_paren_before_context_and_firm_not_demoted():
    q = extract_quantity_fields(
        "Vendor may supply up to the contract maximum. "
        "Agency will purchase of twelve (12) ribbon fiber optic cable per this IFB.",
        title="Furnish and Deliver Twelve (12) Miles of Fiber",
    )
    assert q.get("quantity") == "12"
    assert q.get("quantity_semantic") == "ORDERED_QUANTITY"
    assert q.get("quantity_kind") == "fixed"


def test_bare_ea_and_n_each_of():
    q = extract_quantity_fields(
        "Line schedule: purchase 25 EA radios for field units.",
        title="Radio Equipment Purchase",
    )
    assert q.get("quantity_extracted")
    assert int(float(q["quantity"])) == 25

    q2 = extract_quantity_fields(
        "Provide 3 each of replacement pump cartridges.",
        title="Pump Cartridge Supplies",
    )
    assert q2.get("quantity") == "3"


def test_ifb_title_alone_not_submission_pass():
    item = {
        "title": "IFB 26-0094 - Purchase of One (1) HVAC Split System",
        "solicitation_status": "open",
        "lifecycle_status": "open",
        "requirement_class": "product_supply",
        "record_type": "INDIVIDUAL_SOLICITATION",
        "verified_individual_solicitation": True,
        "quantity": "1",
        "quantity_semantic": "ORDERED_QUANTITY",
        "quantity_kind": "fixed",
        "quantity_evidence": ["One (1)", "affirmative_ordered_quantity_language"],
        "document_set_status": "COMPLETE",
        "document_text": "IFB invitation for bids. Furnish one (1) HVAC unit. " * 20,
        "document_text_len": 400,
        "description": "Purchase of HVAC equipment for Del Norte facility.",
    }
    pipe = evaluate_procurement_pipeline(item, document_text=item["document_text"])
    assert pipe.get("quantity_defined") is True
    bid = evaluate_bid_eligibility(item, pipe, document_text=item["document_text"])
    assert bid["gates"]["active_solicitation"]["status"] == "pass"
    assert bid["gates"]["sufficient_documents"]["status"] == "pass"
    # Owner: IFB title alone ≠ submission instructions
    assert bid["gates"]["submission_instructions"]["status"] == "unknown"
    assert "instrument_title_only" in str(bid["gates"]["submission_instructions"]["evidence"])


def test_real_submission_language_passes():
    item = {
        "title": "Purchase of Two (2) Generators",
        "solicitation_status": "open",
        "requirement_class": "product_supply",
        "record_type": "INDIVIDUAL_SOLICITATION",
        "verified_individual_solicitation": True,
        "quantity": "2",
        "quantity_semantic": "ORDERED_QUANTITY",
        "quantity_kind": "fixed",
        "quantity_evidence": ["Two (2)"],
        "document_set_status": "COMPLETE",
        "document_text": (
            "Submit bids electronically via the portal before the deadline. "
            "Furnish two (2) diesel generators per specifications. "
        )
        * 15,
        "document_text_len": 600,
    }
    pipe = evaluate_procurement_pipeline(item, document_text=item["document_text"])
    bid = evaluate_bid_eligibility(item, pipe, document_text=item["document_text"])
    assert bid["gates"]["submission_instructions"]["status"] == "pass"


def test_title_body_overlap():
    ok = title_body_overlap(
        "Purchase of Chemical Flow Meters",
        "This solicitation is for chemical flow meters including installation options.",
    )
    assert ok["ok"] is True
    bad = title_body_overlap(
        "Helicopter transport services Panther Gap",
        "Welcome to the City purchasing homepage. Cookie consent. Browse all bids.",
    )
    assert bad["ok"] is False
