"""BUILD 002R — mandatory procurement-intelligence acceptance gates (offline)."""

from __future__ import annotations

from m3.research.classify import classify_requirement
from m3.research.commercial_criteria import evaluate_bid_eligibility, evaluate_procurement_pipeline
from m3.research.extract_fields import extract_quantity_fields


def test_a_product_versus_service_confirmed_examples():
    cases = [
        (
            "FURNISHING AND DELIVERY OF TREATED ROCK SALT",
            "Furnishing and Delivery of Treated Rock Salt as required by the Department.",
            "product_supply",
        ),
        (
            "FURNISHING AND DELIVERY OF FERTILIZER, GRASS SEED, AND RELATED LANDSCAPING ITEMS",
            "Furnishing and Delivery of Fertilizer, Grass Seed, and Related Landscaping Items",
            "product_supply",
        ),
        (
            "One (1) Compact Tracked Articulating Boom Lift with Transport Trailer",
            "Purchase of one compact tracked articulating boom lift with transport trailer.",
            "product_supply",
        ),
        (
            "FURNISHING AND DELIVERY OF A HANDHELD LEAD PAINT ANALYZER AND RELATED ITEMS",
            "Furnishing and Delivery of a handheld Lead Paint Analyzer and Related Items",
            "product_supply",
        ),
        (
            "25-0047 - Partnerships for Families Services Request for Proposals #25-0047",
            "Department of Children and Family Services is issuing this RFP for family intervention.",
            "services",
        ),
        (
            "ANNUAL RENEWAL OF QUALIFIED CONTRACTORS LIST PER UNIFORM PUBLIC CONSTRUCTION COST ACCOUNTING UCCAA",
            "inviting licensed contractors to submit for inclusion on the Informal Bidding Contractors List",
            "vendor_qualification",
        ),
        (
            "AMR, N Liberty S, Multi Cnty",
            "The principal items of work and approximate quantities are: Implementation of the Erosion and Sediment Pollution Control Plan",
            "construction",
        ),
        # 002R.2 — product nouns must not imply a goods purchase
        (
            "Engineering and Traffic Surveys Supporting the Town's Speed Limits to Continue Radar Enforcement",
            "The California Vehicle Code requires engineering and traffic surveys. "
            "The selected consultant shall conduct a Townwide Speed Survey.",
            "professional_services",
        ),
        (
            "Climate Controlled Booth Installation - IFB 26C626001",
            "CHP Calexico Commercial Vehicle Enforcement Facility (CVEF) — Climate Controlled Booth Installation",
            "services",
        ),
        (
            "PRE-QUAL FOR FURNITURE INSTALLATION SERVICES",
            "PRE-QUAL FOR FURNITURE INSTALLATION SERVICES",
            "vendor_qualification",
        ),
        (
            "REPAIR OF FUEL SYSTEMS EQUIPMENT, PRE-QUAL OF VENDORS",
            "REPAIR OF FUEL SYSTEMS EQUIPMENT, PRE-QUAL OF VENDORS",
            "vendor_qualification",
        ),
    ]
    for title, desc, expected in cases:
        got = classify_requirement(title, description=desc)["requirement_class"]
        assert got == expected, f"{title}: got {got} expected {expected}"


def test_b_quantity_precision():
    kv = extract_quantity_fields(
        "soliciting bids for the purchase of 35 kV Grounding Switches.",
        title="Grounding Switches 35kV",
    )
    assert kv.get("quantity") is None
    assert not kv.get("quantity_is_ordered")

    hrs = extract_quantity_fields(
        "Contractor will also provide 24 hours per day / 7 day per week telephone technical support.",
        title="QU27003 Fire Protection Services",
    )
    assert hrs.get("quantity") is None
    assert not hrs.get("quantity_is_ordered")

    money = extract_quantity_fields("Estimated contract value $100,000 for supplies.")
    assert money.get("quantity") is None or not money.get("quantity_is_ordered")

    multi = extract_quantity_fields(
        "supply the Lottery with two (2) Narrow-Aisle Reach Trucks, one (1) Sit-Down Rider Truck, "
        "and one (1) Rider Pallet Truck Forklifts and (4) compatible Lithium-Ion Battery Chargers",
        title="IFB Forklifts with Lithium-Ion Battery Chargers",
    )
    assert multi.get("quantity_semantic") == "MULTI_LINE_ITEM"
    assert multi.get("quantity") is None  # never collapse
    lines = multi.get("verified_line_items") or []
    assert len(lines) >= 3
    assert any(str(li.get("quantity")) == "4" for li in lines)

    # Boom lift with trailer: one package — title echoed in body must not duplicate
    boom_title = (
        "One (1) Compact Tracked Articulating Boom Lift with Transport Trailer for Saratoga Region"
    )
    boom_body = (
        f"Title: {boom_title} CR#: 2140049 Agency: Parks Recreation "
        "Category: Vehicles & Equipment"
    )
    boom = extract_quantity_fields(boom_body, title=boom_title)
    assert boom.get("quantity") == "1"
    assert boom.get("quantity_semantic") == "ORDERED_QUANTITY"
    assert boom.get("quantity_semantic") != "MULTI_LINE_ITEM"
    boom_lines = boom.get("verified_line_items") or boom.get("line_items") or []
    assert len(boom_lines) == 1
    # No identical duplicate rows
    snippets = [str(li.get("snippet")) for li in boom_lines]
    assert len(snippets) == len(set(snippets)) or len(boom_lines) == 1


def test_c_rejection_requires_evidence():
    # Missing info must not become confirmed service rejection
    r = classify_requirement("Miscellaneous procurement opportunity", description="")
    assert r["requirement_class"] == "unknown_mixed"
    assert "insufficient_evidence" in (r.get("classification_evidence") or [])


def test_d_readiness_gates_block_construction_and_false_qty():
    construction = {
        "title": "AMR, N Liberty S, Multi Cnty",
        "requirement_class": "construction",
        "solicitation_status": "open",
        "verified_individual_solicitation": True,
        "quantity": "1",
        "quantity_semantic": "ORDERED_QUANTITY",
        "document_text": "Erosion and Sediment Pollution Control Plan - Rock Construction Entrance, 1 EACH",
        "submission_method": "portal",
        "submission_instructions": "submit via eMarketplace",
        "package_status": "COMPLETE",
    }
    pipe = evaluate_procurement_pipeline(construction, document_text=construction["document_text"])
    assert pipe.get("excluded") is True
    assert pipe.get("product_opportunity") is False
    bid = evaluate_bid_eligibility(construction, pipe, document_text=construction["document_text"])
    assert bid.get("ready_to_submit") is not True
    assert bid.get("bid_eligible") is not True


def test_e_invariants_no_invented_quantity_status():
    empty = extract_quantity_fields("No numbers here about how many units are ordered.")
    assert empty.get("quantity_status") == "NOT_FOUND_IN_RETAINED_EVIDENCE"
    assert empty.get("quantity") is None
