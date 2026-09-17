"""Regression tests for live autonomous deal validation behavior."""

from __future__ import annotations

from live_package_completeness import (
    AVAILABLE,
    AUTH_REQUIRED,
    MISSING,
    assess_live_package_completeness,
    is_forbidden_primary,
    selection_uses_forbidden_outcome_fields,
)
from executable_deal_constants import LIVE_IOWA
from transactional_bom import extract_sciquest_product_line_items, extract_delivery_and_terms


def test_iowa_blade_forbidden_as_primary():
    assert is_forbidden_primary(LIVE_IOWA)
    assert not is_forbidden_primary("645-DOTRFB-3046-2027")


def test_award_fields_block_selection():
    assert selection_uses_forbidden_outcome_fields({"award_winner": "Acme"})
    assert not selection_uses_forbidden_outcome_fields({"title": "Seed RFB"})


def test_auth_gated_without_product_id_not_complete():
    r = assess_live_package_completeness(
        documents=[
            {
                "document_class": "SPECIFICATION",
                "access_status": "LOGIN_REQUIRED",
                "document_title": "spec.pdf",
            }
        ],
        line_items=[],
        terms={},
        deadline="10/1/2026, 1:00 PM CDT",
        deadline_status="OPEN",
        auth_barriers=["spec.pdf"],
        has_authoritative_text=False,
    )
    assert r["critical_auth_gated_spec"] is True
    assert r["package_status"] == "PACKAGE_INCOMPLETE"


def test_complete_package_with_tbd_delivery_and_soft_auth():
    lines = [
        {"description": "Big bluestem (Andropogon gerardii) native seed", "quantity": 100, "unit": "LB"},
        {"description": "Switchgrass (Panicum virgatum)", "quantity": 50, "unit": "LB"},
    ]
    r = assess_live_package_completeness(
        documents=[
            {"access_status": "PUBLIC_FETCHED", "document_class": "SOLICITATION", "text_length": 1000},
            {
                "document_class": "SPECIFICATION",
                "access_status": "LOGIN_REQUIRED",
                "document_title": "Wildflower Spec.pdf",
            },
        ],
        line_items=lines,
        terms={
            "delivery_location": {"value": "TBD_BY_BUYER_PRIOR_TO_DELIVERY"},
            "bid_deadline": {"value": "9/28/2026, 1:00 PM CDT"},
            "FOB_terms": {"value": "F.O.B Destination"},
        },
        deadline="9/28/2026, 1:00 PM CDT",
        deadline_status="OPEN",
        auth_barriers=["Wildflower Spec.pdf"],
        title="Wildflower Seed",
        has_authoritative_text=True,
    )
    assert r["package_status"] == "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS"
    assert r["soft_auth_gated_attachment"] is True
    assert r["critical_auth_gated_spec"] is False
    assert r["categories"]["delivery_destination"]["status"] == AVAILABLE


def test_unknown_quantity_not_complete():
    r = assess_live_package_completeness(
        documents=[{"access_status": "PUBLIC_FETCHED", "text_length": 10}],
        line_items=[{"description": "Widget", "quantity": None}],
        terms={"delivery_location": {"value": "Ames, IA"}, "bid_deadline": {"value": "10/1/2026"}},
        deadline="10/1/2026",
        deadline_status="OPEN",
        has_authoritative_text=True,
    )
    assert r["categories"]["quantities"]["status"] != AVAILABLE
    assert r["score"]["eligible_for_complete_validation"] is False


def test_ls_uom_extracted_from_sciquest():
    text = """
Product Line Items
P1
IFB: Wheelchair Lift
1
LS - Lump Sum
Lumpsum cost to provide equipment
P2
Warranty for Products
1
YR - Year
"""
    lines = extract_sciquest_product_line_items(text, source_document="t")
    assert len(lines) >= 1
    assert lines[0]["quantity"] == 1.0
    assert lines[0]["unit_of_measure"] == "LS"


def test_tbd_delivery_extracted():
    text = "Deliveries shall be F.O.B Destination.\nDelivery Information\nDelivery information will be provided prior to required delivery date.\nClose\n9/28/2026, 1:00 PM CDT\n"
    terms = extract_delivery_and_terms(text, source_document="t")
    assert terms["delivery_location"]["value"] == "TBD_BY_BUYER_PRIOR_TO_DELIVERY"


def test_complete_outranks_incomplete_for_validation():
    complete = assess_live_package_completeness(
        documents=[{"access_status": "PUBLIC_FETCHED", "text_length": 50}],
        line_items=[{"description": "Native seed mix species A detailed", "quantity": 10, "unit": "LB"}],
        terms={
            "delivery_location": {"value": "Ames, IA"},
            "bid_deadline": {"value": "10/1/2026, 1:00 PM CDT"},
        },
        deadline="10/1/2026, 1:00 PM CDT",
        deadline_status="OPEN",
        has_authoritative_text=True,
    )
    incomplete = assess_live_package_completeness(
        documents=[{"document_class": "SPECIFICATION", "access_status": "LOGIN_REQUIRED"}],
        line_items=[],
        auth_barriers=["spec"],
        deadline=None,
    )
    assert complete["score"]["eligible_for_complete_validation"] is True
    assert incomplete["score"]["eligible_for_complete_validation"] is False
    assert complete["score"]["points"] > incomplete["score"]["points"]
