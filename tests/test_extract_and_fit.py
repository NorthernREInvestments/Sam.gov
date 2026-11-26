"""Build 002I quantity semantics + procurement fit stages."""

from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import extract_quantity_fields
from m3.research.package import classify_url_kind, discover_delaware_mmp_documents


def test_extract_quantity_explicit():
    r = extract_quantity_fields("Quantity: 250 each of battery chargers for warehouse use.")
    assert r["quantity_extracted"]
    assert r["quantity"] == "250"
    assert r["quantity_semantic"] == "ORDERED_QUANTITY"
    assert r["quantity_is_ordered"]


def test_extract_does_not_invent():
    r = extract_quantity_fields("Office supplies under an indefinite quantity / requirements contract.")
    assert not r["quantity_extracted"]
    assert r["quantity_kind"] == "indefinite"


def test_boilerplate_as_needed_does_not_block_ordered_qty():
    r = extract_quantity_fields(
        "Vendor instructions: leave blank as needed.\nQuantity: 30 EA line_item:Nike Practice Soccer Balls"
    )
    assert r["quantity_extracted"]
    assert r["quantity"] == "30"
    assert r["quantity_semantic"] == "ORDERED_QUANTITY"


def test_reject_gvwr_weight_as_quantity():
    r = extract_quantity_fields(
        "",
        title="BUCKET TRUCK, MINIMUM 19,500 LB. GVWR CREW CAB AND CHASSIS",
    )
    assert not r["quantity_extracted"]


def test_per_cup_is_package_not_ordered():
    """002H false fit: (250 PER CUP) is packaging/capacity, not ordered total."""
    r = extract_quantity_fields("", title="LIVE WORMS, WAX (250 PER CUP)")
    assert not r["quantity_is_ordered"]
    assert not r["quantity_extracted"]
    assert r.get("quantity_semantic") == "PACKAGE_QUANTITY" or (
        r.get("ambiguous_quantity_candidates")
        and r["ambiguous_quantity_candidates"][0]["semantic"] == "PACKAGE_QUANTITY"
    )


def test_paren_unit_alone_is_ambiguous_not_ordered():
    """002H false fit: (300 UNIT) without affirmative ordered language is unknown."""
    r = extract_quantity_fields("", title="139903R1 HYDRANT PRESSURE RECORDER (300 UNIT)")
    assert not r["quantity_is_ordered"]
    assert not r["quantity_extracted"]
    assert r.get("quantity_semantic") == "UNKNOWN_NUMERIC_VALUE" or (
        r.get("ambiguous_quantity_candidates")
        and r["ambiguous_quantity_candidates"][0]["semantic"] == "UNKNOWN_NUMERIC_VALUE"
    )


def test_furnish_n_units_is_ordered():
    r = extract_quantity_fields("The District will purchase 72 golf carts for parks maintenance.")
    assert r["quantity_extracted"]
    assert r["quantity"] == "72"
    assert r["quantity_semantic"] == "ORDERED_QUANTITY"


def test_five_paren_title_is_ordered():
    r = extract_quantity_fields(
        "", title="2026-011 Five (5) New Custom Class A Pumper Apparatus"
    )
    assert r["quantity_extracted"]
    assert r["quantity"] == "5"
    assert r["quantity_semantic"] == "ORDERED_QUANTITY"


def test_purchase_of_n_is_ordered():
    r = extract_quantity_fields(
        "", title="IWQ 141936, Purchase of 16 Trenching Box Systems"
    )
    assert r["quantity_extracted"]
    assert r["quantity"] == "16"


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
            "quantity_semantic": "ORDERED_QUANTITY",
            "quantity_evidence": ["Quantity: 12 each"],
        }
    )
    assert pipe["stage"] == STAGE_C
    assert pipe["quantity_defined"]


def test_pipeline_rejects_package_quantity_as_fit():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "LIVE WORMS, WAX (250 PER CUP)",
            "requirement_class": "product_supply",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
        }
    )
    assert pipe["stage"] == STAGE_B
    assert not pipe["quantity_defined"]


def test_pipeline_rejects_ambiguous_unit_paren():
    pipe = evaluate_procurement_pipeline(
        {
            "title": "HYDRANT PRESSURE RECORDER (300 UNIT)",
            "requirement_class": "product_supply",
            "solicitation_status": "open",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
        }
    )
    # Stage B = product opportunity without firm Stage-C quantity
    assert pipe["stage"] in {STAGE_B, "VERIFIED_PRODUCT_OPPORTUNITY"}
    assert not pipe["quantity_defined"]


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


def test_delaware_detail_url_is_detail_not_listing():
    assert (
        classify_url_kind("https://mmp.delaware.gov/Bids/Details/9397") == "detail"
    )


def test_delaware_document_list_public(monkeypatch):
    """Uses live public endpoint when network available; otherwise skip gracefully."""
    docs = discover_delaware_mmp_documents("https://mmp.delaware.gov/Bids/Details/9397")
    if not docs:
        # Offline / WAF in CI — structure still unit-tested via URL classify
        return
    assert any("bidcondocs.delaware.gov" in (d.get("source_url") or "") for d in docs)
    assert any((d.get("source_url") or "").lower().endswith((".pdf", ".xlsx")) for d in docs)
