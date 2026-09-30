"""BUILD 5 — Award/product fact projection tests."""

from __future__ import annotations

from federal_dla_product_constants import ID_EXACT_NSN, READY_COMMERCIAL_RESEARCH
from m3_award_product_projection import (
    BUILD_TAG,
    InMemoryAwardBackend,
    extract_award_product_identity,
    normalize_award,
    project_award_product_facts,
)
from m3_intelligence_graph_read import assemble_intelligence_graph
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import INTEL_VALIDATED


def _opp_with_nsn(**extra):
    base = {
        "canonical_id": "sol:SPE7M126AW1:dla",
        "notice_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "external_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M126AW1",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "readiness_state": READY_COMMERCIAL_RESEARCH,
        "product_identity": {"identity_state": ID_EXACT_NSN, "confidence": "HIGH"},
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "has_exact_nsn": True,
            "has_cage": True,
            "cage": "12345",
            "fields": {
                "nsn": {
                    "value": "4320-01-243-1951",
                    "confidence": "HIGH",
                    "evidence_source": "title",
                    "evidence_snippet": "NSN 4320-01-243-1951",
                },
                "cage": {
                    "value": "12345",
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "CAGE 12345",
                },
            },
        },
    }
    base.update(extra)
    return base


def test_award_with_exact_nsn_creates_demand_evidence():
    backend = InMemoryAwardBackend()
    awards = [
        {
            "award_id": "CONT-001",
            "nsn": "4320-01-243-1951",
            "identity_confidence": "HIGH",
            "Award Amount": 15000,
            "Awarding Agency": "DLA Aviation",
            "Recipient Name": "Acme Defense LLC",
            "Start Date": "2024-06-01",
            "source": "usaspending:CONT-001",
        }
    ]
    result = project_award_product_facts(awards=awards, backend=backend)
    assert result["projected"] is True
    assert result["awards_projected"] == 1
    assert result["demand_evidence"]
    dem = result["demand_evidence"][0]
    assert dem["kind"] == "ProductDemandEvidence"
    assert dem["agency"] == "DLA Aviation"
    assert dem["value"] == 15000.0
    assert dem["date"] == "2024-06-01"
    assert dem["award_source"] == "usaspending:CONT-001"
    assert dem["confidence"] == "HIGH"
    assert result["supplier_relationships"]
    assert result["supplier_relationships"][0]["relationship"] == "HISTORICAL_AWARDEE"
    assert result["supplier_relationships"][0]["supplier_name"] == "Acme Defense LLC"
    pid = result["knowledge_product_ids"][0]
    specs = backend.products[pid]["specifications_json"]
    assert specs["nsn"] == "4320-01-243-1951"
    assert len(specs["demand_evidence"]) == 1
    assert "DLA Aviation" in specs["buying_agencies"]


def test_description_only_award_not_promoted():
    backend = InMemoryAwardBackend()
    awards = [
        {
            "award_id": "WEAK-1",
            "Description": "Miscellaneous industrial supplies and pumps for maintenance",
            "Award Amount": 99000,
            "Awarding Agency": "Some Agency",
            "Recipient Name": "Vendor Co",
            "Start Date": "2023-01-01",
        }
    ]
    # No opportunity row — description alone must not invent product
    result = project_award_product_facts(awards=awards, backend=backend)
    assert result["projected"] is False
    assert result["awards_projected"] == 0
    assert any(s.get("description_only") or "description" in str(s.get("reason")) for s in result["skipped"])
    assert backend.products == {}


def test_same_award_product_does_not_duplicate():
    backend = InMemoryAwardBackend()
    awards = [
        {
            "award_id": "CONT-DUP",
            "nsn": "4320-01-243-1951",
            "identity_confidence": "HIGH",
            "award_amount": 10000,
            "agency": "DLA",
            "awardee": "Acme",
            "award_date": "2024-01-01",
            "source": "usaspending:CONT-DUP",
        }
    ]
    a = project_award_product_facts(awards=awards, backend=backend)
    b = project_award_product_facts(awards=awards, backend=backend)
    assert a["awards_projected"] == 1
    assert b["awards_projected"] == 0
    assert any(s.get("reason") == "duplicate_award_product" for s in b["skipped"])
    assert len(backend.products) == 1
    specs = backend.products[a["knowledge_product_ids"][0]]["specifications_json"]
    assert len(specs["demand_evidence"]) == 1


def test_evidence_source_date_value_retained():
    backend = InMemoryAwardBackend()
    awards = [
        {
            "award_id": "EV-9",
            "nsn": "4320-01-243-1951",
            "identity_confidence": "VALIDATED",
            "award_amount": 12345.67,
            "agency": "DLA Land",
            "buyer": "DLA Land Warren",
            "awardee": "Parts Co",
            "award_date": "2025-03-15",
            "quantity": 10,
            "source": "usaspending:EV-9",
        }
    ]
    result = project_award_product_facts(awards=awards, backend=backend)
    dem = result["demand_evidence"][0]
    assert dem["award_id"] == "EV-9"
    assert dem["date"] == "2025-03-15"
    assert dem["value"] == 12345.67
    assert dem["buyer"] == "DLA Land Warren"
    assert dem["quantity"] == 10
    assert dem["evidence"]["source"] == "usaspending:EV-9"
    assert dem["evidence"]["nsn"] == "4320-01-243-1951"


def test_opportunity_linked_award_uses_validated_product():
    """Awards without NSN field still project when opportunity has validated identity."""
    backend = InMemoryAwardBackend()
    row = _opp_with_nsn(
        historical_awards=[
            {
                "award_id": "LINK-1",
                "award_amount": 8000,
                "agency": "DLA",
                "awardee": "Linked Vendor",
                "award_date": "2024-08-01",
                "source": "row.historical_awards",
            }
        ]
    )
    result = project_award_product_facts(row, backend=backend)
    assert result["projected"] is True
    assert result["demand_evidence"][0]["agency"] == "DLA"
    assert "opportunity_validated" in (
        extract_award_product_identity(row["historical_awards"][0], opportunity_row=row).get("reason") or ""
    )


def test_graph_historical_shows_award_projection():
    row = _opp_with_nsn(
        award_product_projection={
            "build": BUILD_TAG,
            "projected_at": "2026-09-18T18:00:00+00:00",
            "knowledge_product_ids": [7],
            "demand_count": 1,
            "demand_evidence": [
                {
                    "kind": "ProductDemandEvidence",
                    "agency": "DLA Aviation",
                    "value": 15000,
                    "date": "2024-06-01",
                    "award_source": "usaspending:CONT-001",
                    "confidence": "HIGH",
                }
            ],
            "supplier_relationships": [
                {"supplier_name": "Acme Defense LLC", "relationship": "HISTORICAL_AWARDEE"}
            ],
            "buying_agencies": ["DLA Aviation"],
        }
    )
    resolver = OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    g = assemble_intelligence_graph(row, identity=ident, opportunity_id=row["canonical_id"])
    hist = g["historical_intelligence"]
    assert hist["status"] == INTEL_VALIDATED
    assert hist["facts"]["demand_evidence_count"] == 1
    assert "DLA Aviation" in hist["facts"]["buying_agencies"]
    assert "Acme Defense LLC" in hist["facts"]["historical_suppliers"]
    assert any(e.get("field") == "product_demand" for e in hist["evidence"])


def test_normalize_usaspending_shape():
    n = normalize_award(
        {
            "Award ID": "X1",
            "Award Amount": "12,000.50",
            "Awarding Agency": "VA",
            "Recipient Name": "Co",
            "Start Date": "2022-01-01",
        }
    )
    assert n["award_id"] == "X1"
    assert n["award_amount"] == 12000.50
    assert n["agency"] == "VA"
    assert n["awardee"] == "Co"


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
