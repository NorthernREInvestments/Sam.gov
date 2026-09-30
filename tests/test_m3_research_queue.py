"""BUILD 4 — Graph-driven research queue tests."""

from __future__ import annotations

from federal_dla_product_constants import ID_EXACT_NSN, ID_AMBIGUOUS, READY_COMMERCIAL_RESEARCH
from m3_intelligence_graph_read import assemble_intelligence_graph
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import INTEL_UNKNOWN, INTEL_VALIDATED
from m3_research_queue import (
    BUILD_TAG,
    ECONOMICS,
    FINANCING,
    HISTORICAL,
    PRODUCT_IDENTITY,
    STATUS_COMPLETE,
    SUPPLIER,
    build_research_queue,
    research_items_from_graph,
    set_research_item_status,
)


def _product_known_supplier_unknown(**extra):
    base = {
        "canonical_id": "sol:SPE7M126RQ1:dla",
        "notice_id": "cccccccccccccccccccccccccccccccc",
        "external_id": "cccccccccccccccccccccccccccccccc",
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M126RQ1",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "lifecycle": "RESEARCH",
        "readiness_state": READY_COMMERCIAL_RESEARCH,
        "package_access": "PUBLIC",
        "updated_at": "2026-09-18T12:00:00+00:00",
        "product_identity": {"identity_state": ID_EXACT_NSN, "confidence": "HIGH"},
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "quantity": 10,
            "unit_of_issue": "EA",
            "has_exact_nsn": True,
            "fields": {
                "nsn": {
                    "value": "4320-01-243-1951",
                    "confidence": "HIGH",
                    "evidence_source": "title",
                    "evidence_snippet": "NSN 4320-01-243-1951",
                },
                "quantity": {
                    "value": 10,
                    "confidence": "HIGH",
                    "evidence_source": "title",
                    "evidence_snippet": "Qty 10",
                },
                "unit_of_issue": {
                    "value": "EA",
                    "confidence": "HIGH",
                    "evidence_source": "title",
                    "evidence_snippet": "EA",
                },
            },
        },
        "historical_award_amount": 18000,
        "government_price_history": {"confidence": "HIGH"},
    }
    base.update(extra)
    return base


def _fully_validated(**extra):
    row = _product_known_supplier_unknown(
        supplier_intelligence={
            "kind": "M3SupplierIntelligence",
            "ACQUISITION_COST_CONFIDENCE": "HIGH",
            "Supply_chain": {
                "all_channels": [
                    {"company": "Acme", "role": "distributor", "validation_status": "VALIDATED"}
                ]
            },
            "Pricing_evidence": {
                "primary_level": "LEVEL_1",
                "items": [{"source": "quote", "price": 100, "confidence": "HIGH"}],
            },
        },
        procurement_path={"selected_path_id": "supplier_quote"},
        funding_requirement={"status": "VERIFIED", "capital_amount": 4000, "confidence": "HIGH"},
        funding_status="VERIFIED",
        deal_economics={
            "kind": "M3DealEconomics",
            "PRICE_CONFIDENCE": "HIGH",
            "PROFIT_TARGET_STATUS": "ON_TARGET",
            "DEAL_ECONOMICS_PROFILE": {
                "Revenue": 25000,
                "Projected_profit": 8000,
                "Current_acquisition_cost": 12000,
                "pricing_source": "quote",
            },
        },
    )
    row.update(extra)
    return row


def _ambiguous_low(**extra):
    base = {
        "canonical_id": "sol:SPE7M126RQ0:dla",
        "notice_id": "dddddddddddddddddddddddddddddddd",
        "external_id": "dddddddddddddddddddddddddddddddd",
        "source_id": "fed_sam_contract_opportunities",
        "title": "Ambiguous supplies",
        "agency": "DLA",
        "lifecycle": "RESEARCH",
        "product_identity": {"identity_state": ID_AMBIGUOUS},
        "dla_product_structure": {"fields": {}},
        # High contract-ish value alone should not win ranking
        "deal_economics": {
            "kind": "M3DealEconomics",
            "PRICE_CONFIDENCE": "UNKNOWN",
            "DEAL_ECONOMICS_PROFILE": {"Revenue": 500000, "Projected_profit": "UNKNOWN"},
        },
    }
    base.update(extra)
    return base


def _graph(row):
    resolver = OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    return assemble_intelligence_graph(row, identity=ident, opportunity_id=row["canonical_id"])


def test_unknown_supplier_creates_supplier_research():
    row = _product_known_supplier_unknown()
    items = research_items_from_graph(row, _graph(row), status_index={"by_key": {}})
    types = {i["research_type"] for i in items}
    assert SUPPLIER in types
    supplier = next(i for i in items if i["research_type"] == SUPPLIER)
    assert supplier["status"] == "NEW"
    assert supplier["missing_information"]
    assert "supplier" in supplier["why_this_matters"].lower() or "Supplier" in supplier["why_this_matters"]
    assert supplier["recommended_action"]
    assert supplier["opportunity_id"] == row["canonical_id"]


def test_unknown_financing_creates_financing_research():
    row = _product_known_supplier_unknown()
    items = research_items_from_graph(row, _graph(row), status_index={"by_key": {}})
    types = {i["research_type"] for i in items}
    assert FINANCING in types
    fin = next(i for i in items if i["research_type"] == FINANCING)
    assert fin["graph_status"] in {INTEL_UNKNOWN, "RESEARCH_REQUIRED", "POSSIBLE"} or fin["status"] == "NEW"


def test_validated_nodes_do_not_create_unnecessary_research():
    row = _fully_validated()
    g = _graph(row)
    assert g["product_identity"]["status"] == INTEL_VALIDATED
    assert g["supplier_intelligence"]["status"] == INTEL_VALIDATED
    items = research_items_from_graph(row, g, status_index={"by_key": {}})
    types = {i["research_type"] for i in items}
    assert PRODUCT_IDENTITY not in types
    assert SUPPLIER not in types
    assert HISTORICAL not in types
    assert ECONOMICS not in types


def test_high_potential_ranks_above_high_value_ambiguous():
    strong = _product_known_supplier_unknown(canonical_id="sol:STRONG:dla")
    weak = _ambiguous_low(canonical_id="sol:WEAK:dla")
    q = build_research_queue(
        rows=[weak, strong],
        identity_resolver=OpportunityIdentityResolver(),
        status_index={"by_key": {}},
        limit=50,
    )
    assert q["kind"] == "M3GraphResearchQueue"
    assert q["build"] == BUILD_TAG
    # First supplier research for strong product-known opp should outrank weak product research
    supplier_items = [i for i in q["items"] if i["research_type"] == SUPPLIER]
    assert supplier_items
    assert supplier_items[0]["opportunity_id"] == "sol:STRONG:dla"
    # Ensure ranking note present
    assert "not contract value alone" in q["priority_note"].lower()


def test_complete_status_suppresses_item():
    row = _product_known_supplier_unknown()
    index = {"by_key": {}}
    set_research_item_status(
        row["canonical_id"], SUPPLIER, STATUS_COMPLETE, status_index=index, persist=False
    )
    items = research_items_from_graph(row, _graph(row), status_index=index)
    assert SUPPLIER not in {i["research_type"] for i in items}


def test_queue_item_fields_present():
    row = _product_known_supplier_unknown()
    items = research_items_from_graph(row, _graph(row), status_index={"by_key": {}})
    assert items
    for i in items:
        for field in (
            "opportunity_id",
            "research_type",
            "priority",
            "reason",
            "missing_information",
            "recommended_action",
            "created_at",
            "status",
        ):
            assert field in i
        assert i["why_this_matters"]


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
