"""BUILD 8 — Supplier–product graph hardening tests."""

from __future__ import annotations

from m3_intelligence_graph_read import assemble_intelligence_graph
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import INTEL_VALIDATED
from m3_supplier_product_graph import (
    BUILD_TAG,
    DISTRIBUTOR,
    EDGE_VALIDATED,
    HISTORICAL_GOVERNMENT_SUPPLIER,
    MANUFACTURER,
    InMemorySupplierGraphBackend,
    collect_supplier_candidates,
    harden_supplier_product_graph,
)


def _product_row(**extra):
    row = {
        "canonical_id": "sol:SPE7M126SP1:dla",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "knowledge_product_id": 21,
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "cage": "12345",
            "oem": "Acme Pump Co",
            "fields": {
                "nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"},
                "cage": {"value": "12345", "confidence": "HIGH"},
                "oem": {"value": "Acme Pump Co", "confidence": "HIGH"},
            },
        },
        "product_identity": {"identity_state": "EXACT_NSN"},
    }
    row.update(extra)
    return row


def test_historical_awardee_creates_supplier_relationship():
    row = _product_row(
        award_product_projection={
            "knowledge_product_ids": [21],
            "supplier_relationships": [
                {
                    "supplier_name": "Acme Defense LLC",
                    "supplier_id": 9,
                    "relationship": "HISTORICAL_AWARDEE",
                    "confidence": "HIGH",
                    "evidence": {"award_id": "CONT-88", "award_amount": 15000, "award_date": "2024-06-01"},
                }
            ],
        }
    )
    backend = InMemorySupplierGraphBackend()
    result = harden_supplier_product_graph(row, backend=backend)
    assert result["projected"] is True
    assert result["outreach_triggered"] is False
    types = {e["relationship_type"] for e in result["edges"]}
    assert HISTORICAL_GOVERNMENT_SUPPLIER in types
    hist = next(e for e in result["edges"] if e["relationship_type"] == HISTORICAL_GOVERNMENT_SUPPLIER)
    assert hist["supplier_name"] == "Acme Defense LLC"
    assert hist["confidence"] == EDGE_VALIDATED
    assert hist["evidence"]
    assert hist["product_nsn"] == "4320-01-243-1951"


def test_verified_offer_creates_supplier_edge():
    row = _product_row(
        supplier_offers=[
            {
                "supplier_name": "Graybar",
                "role": "DISTRIBUTOR",
                "verification_status": "VALIDATED",
                "source_type": "FORMAL_QUOTE",
                "unit_price": 120.0,
                "temporal_class": "CURRENT",
                "quote_number": "Q-100",
                "source_reference": "Q-100",
            }
        ]
    )
    backend = InMemorySupplierGraphBackend()
    result = harden_supplier_product_graph(row, backend=backend)
    assert result["projected"] is True
    assert any(e["relationship_type"] == DISTRIBUTOR and e["supplier_name"] == "Graybar" for e in result["edges"])
    assert any(o.get("source_type") == "FORMAL_QUOTE" or o.get("verification_status") == "VALIDATED" for o in backend.offers)


def test_weak_evidence_not_promoted():
    row = _product_row(
        supplier_intelligence={
            "kind": "M3SupplierIntelligence",
            "ACQUISITION_COST_CONFIDENCE": "LOW",
            "Supply_chain": {
                "all_channels": [
                    {"company": "Random Scrape Co", "role": "DISTRIBUTOR", "confidence": "MEDIUM"}
                ]
            },
        }
    )
    # Remove manufacturer fields to isolate weak channel
    row["dla_product_structure"] = {
        "nsn": "4320-01-243-1951",
        "has_exact_nsn": True,
        "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
    }
    cands = collect_supplier_candidates(row)
    assert not any(c["supplier_name"] == "Random Scrape Co" for c in cands)
    backend = InMemorySupplierGraphBackend()
    result = harden_supplier_product_graph(row, backend=backend)
    assert not any(e.get("supplier_name") == "Random Scrape Co" for e in result["edges"])


def test_same_supplier_product_does_not_duplicate():
    row = _product_row(
        award_product_projection={
            "supplier_relationships": [
                {
                    "supplier_name": "Acme Defense LLC",
                    "confidence": "HIGH",
                    "evidence": {"award_id": "CONT-1", "award_amount": 1000},
                }
            ]
        }
    )
    backend = InMemorySupplierGraphBackend()
    a = harden_supplier_product_graph(row, backend=backend)
    b = harden_supplier_product_graph(row, backend=backend)
    hist_edges = [e for e in backend.edges.values() if e["relationship_type"] == HISTORICAL_GOVERNMENT_SUPPLIER]
    assert len(hist_edges) == 1
    assert a["edges_created"] >= 1
    assert b["edges_created"] == 0
    assert b["edges_updated"] >= 1


def test_manufacturer_promoted_with_cage():
    row = _product_row()
    backend = InMemorySupplierGraphBackend()
    result = harden_supplier_product_graph(row, backend=backend)
    assert any(e["relationship_type"] == MANUFACTURER and e["supplier_name"] == "Acme Pump Co" for e in result["edges"])


def test_graph_read_shows_supplier_edges():
    row = _product_row(
        supplier_product_graph={
            "build": BUILD_TAG,
            "projected_at": "2026-09-18T20:00:00+00:00",
            "edges": [
                {
                    "edge_id": "nsn:4320-01-243-1951|acme defense llc|HISTORICAL_GOVERNMENT_SUPPLIER",
                    "supplier_name": "Acme Defense LLC",
                    "relationship_type": HISTORICAL_GOVERNMENT_SUPPLIER,
                    "confidence": EDGE_VALIDATED,
                    "source": "award_product_projection",
                }
            ],
        }
    )
    resolver = OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    g = assemble_intelligence_graph(row, identity=ident, opportunity_id=row["canonical_id"])
    sup = g["supplier_intelligence"]
    assert sup["status"] == INTEL_VALIDATED
    assert sup["facts"]["graph_edge_count"] == 1
    assert any(e.get("supplier_name") == "Acme Defense LLC" for e in sup["facts"]["supplier_product_edges"])


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-supplier-product-graph")
