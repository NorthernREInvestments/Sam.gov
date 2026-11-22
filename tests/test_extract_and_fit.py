"""Build 002H quantity extraction + procurement fit stages."""

from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import extract_quantity_fields


def test_extract_quantity_explicit():
    r = extract_quantity_fields("Quantity: 250 each of battery chargers for warehouse use.")
    assert r["quantity_extracted"]
    assert r["quantity"] == "250"
    assert r["quantity_kind"] in {"fixed", "estimated", "unknown"}


def test_extract_does_not_invent():
    r = extract_quantity_fields("Office supplies as needed for the department.")
    assert not r["quantity_extracted"]
    assert r["quantity_kind"] == "indefinite"


def test_reject_gvwr_weight_as_quantity():
    r = extract_quantity_fields(
        "",
        title="BUCKET TRUCK, MINIMUM 19,500 LB. GVWR CREW CAB AND CHASSIS",
    )
    assert not r["quantity_extracted"]


def test_accept_paren_unit_quantity():
    r = extract_quantity_fields("", title="LIVE WORMS, WAX (250 PER CUP)")
    assert r["quantity"] == "250"


def test_pipeline_fit_with_quantity():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "Battery Testing Kit",
            "requirement_class": "product_supply",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
            "quantity": "12",
            "quantity_kind": "fixed",
            "quantity_evidence": ["Quantity: 12 each"],
        }
    )
    assert pipe["stage"] == STAGE_C
    assert pipe["quantity_defined"]


def test_pipeline_product_without_qty_is_stage_b():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "Copper Bus Pipe",
            "requirement_class": "product_supply",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
        }
    )
    assert pipe["stage"] == STAGE_B
    assert "quantity_unknown" in pipe["unknown_flags"]


def test_pipeline_extracts_from_document_text():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "Furnish valves",
            "requirement_class": "product_supply",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
        },
        document_text="The City will purchase Quantity: 40 EA of butterfly valves per IFB.",
    )
    assert pipe["quantity_defined"]
    assert pipe["stage"] == STAGE_C


def test_services_excluded():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "Janitorial services",
            "requirement_class": "services",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
        }
    )
    assert pipe["excluded"]
