"""BUILD 2 — Product fact projection tests."""

from __future__ import annotations

from federal_dla_product_constants import (
    ID_AMBIGUOUS,
    ID_CATEGORY_IDENTIFIED,
    ID_EXACT_NSN,
    ID_EXACT_OEM_PART,
    ID_UNKNOWN,
    READY_COMMERCIAL_RESEARCH,
)
from m3_product_fact_projection import (
    INTEL_UNKNOWN,
    INTEL_VALIDATED,
    InMemoryProjectionBackend,
    extract_promotable_facts,
    is_eligible_for_projection,
    map_to_intelligence_state,
    project_opportunity_product_facts,
)


def _row_nsn(**extra):
    base = {
        "canonical_id": "sol:SPE7M126T1:dla",
        "notice_id": "dddddddddddddddddddddddddddddddd",
        "external_id": "dddddddddddddddddddddddddddddddd",
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M126T1",
        "agency": "DLA",
        "title": "NSN 4320-01-243-1951 PUMP",
        "readiness_state": READY_COMMERCIAL_RESEARCH,
        "product_identity": {"identity_state": ID_EXACT_NSN, "candidates": []},
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "part_number": None,
            "cage": "12345",
            "quantity": 10,
            "unit_of_issue": "EA",
            "has_exact_nsn": True,
            "has_exact_pn": False,
            "has_cage": True,
            "has_quantity": True,
            "has_uoi": True,
            "fields": {
                "nsn": {
                    "value": "4320-01-243-1951",
                    "confidence": "HIGH",
                    "evidence_source": "title+description",
                    "evidence_snippet": "NSN 4320-01-243-1951",
                    "extraction_method": "regex",
                },
                "cage": {
                    "value": "12345",
                    "confidence": "HIGH",
                    "evidence_source": "title+description",
                    "evidence_snippet": "CAGE 12345",
                    "extraction_method": "regex",
                },
                "quantity": {
                    "value": 10,
                    "confidence": "HIGH",
                    "evidence_source": "title+description",
                    "evidence_snippet": "Qty 10",
                    "extraction_method": "regex",
                },
                "unit_of_issue": {
                    "value": "EA",
                    "confidence": "HIGH",
                    "evidence_source": "title+description",
                    "evidence_snippet": "UI EA",
                    "extraction_method": "regex",
                },
            },
        },
    }
    base.update(extra)
    return base


def test_nsn_part_manufacturer_cage_promotion():
    backend = InMemoryProjectionBackend()
    row = _row_nsn(
        dla_product_structure={
            **_row_nsn()["dla_product_structure"],
            "part_number": "ABC-99-XYZ",
            "has_exact_pn": True,
            "oem": "ACME Pumps",
            "fields": {
                **_row_nsn()["dla_product_structure"]["fields"],
                "part_number": {
                    "value": "ABC-99-XYZ",
                    "confidence": "HIGH",
                    "evidence_source": "description",
                    "evidence_snippet": "P/N ABC-99-XYZ",
                    "extraction_method": "regex",
                },
                "oem": {
                    "value": "ACME Pumps",
                    "confidence": "MEDIUM",
                    "evidence_source": "description",
                    "evidence_snippet": "Manufacturer: ACME Pumps",
                    "extraction_method": "regex",
                },
            },
        },
        product_identity={"identity_state": ID_EXACT_OEM_PART},
    )
    # With NSN present, identity can stay EXACT_NSN — still promote PN/CAGE/OEM with cage
    row["product_identity"] = {"identity_state": ID_EXACT_NSN}
    out = project_opportunity_product_facts(row, backend=backend)
    assert out["projected"] is True
    assert out["knowledge_product_id"]
    types = {f["fact_type"] for f in out["facts"]}
    assert "nsn" in types
    assert "part_number" in types
    assert "cage" in types
    assert "manufacturer" in types  # cage present allows OEM promote
    prod = backend.products[out["knowledge_product_id"]]
    assert prod["specifications_json"]["nsn"] == "4320-01-243-1951"
    assert prod["part_number"] == "ABC-99-XYZ"
    assert prod["manufacturer"] == "ACME Pumps"
    assert prod["verification_status"] == "VALIDATED"


def test_ambiguous_and_low_confidence_not_promoted():
    backend = InMemoryProjectionBackend()
    row = {
        "canonical_id": "sol:WEAK1:x",
        "title": "Miscellaneous supplies",
        "readiness_state": "DISCOVERED_ONLY",
        "product_identity": {"identity_state": ID_AMBIGUOUS},
        "dla_product_structure": {
            "has_exact_nsn": False,
            "has_exact_pn": True,
            "part_number": "Number",  # false token
            "fields": {
                "part_number": {
                    "value": "Number",
                    "confidence": "MEDIUM",
                    "evidence_source": "title_token",
                    "evidence_snippet": "Number",
                }
            },
        },
    }
    assert is_eligible_for_projection(row)["eligible"] is False
    facts = extract_promotable_facts(row)
    assert facts == []
    out = project_opportunity_product_facts(row, backend=backend, force=True)
    assert out["projected"] is False
    assert out["skipped_reason"] == "no_promotable_facts"
    assert backend.products == {}

    cat = {
        "canonical_id": "sol:CAT1:x",
        "product_identity": {"identity_state": ID_CATEGORY_IDENTIFIED},
        "dla_product_structure": {"nomenclature": "PUMP", "has_exact_nsn": False, "has_exact_pn": False},
        "readiness_state": READY_COMMERCIAL_RESEARCH,
    }
    out2 = project_opportunity_product_facts(cat, backend=backend, force=True)
    assert out2["projected"] is False


def test_same_nsn_does_not_duplicate_knowledge_product():
    backend = InMemoryProjectionBackend()
    row1 = _row_nsn()
    row2 = _row_nsn(
        canonical_id="sol:SPE7M126T2:dla",
        solicitation_number="SPE7M126T2",
        notice_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        external_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    a = project_opportunity_product_facts(row1, backend=backend)
    b = project_opportunity_product_facts(row2, backend=backend)
    assert a["projected"] and b["projected"]
    assert a["knowledge_product_id"] == b["knowledge_product_id"]
    assert a["knowledge_product_created"] is True
    assert b["knowledge_product_created"] is False
    assert len(backend.products) == 1
    uids = backend.products[a["knowledge_product_id"]]["specifications_json"].get("opportunity_uids") or []
    assert len(uids) >= 1


def test_evidence_source_snippet_timestamp_retained():
    backend = InMemoryProjectionBackend()
    out = project_opportunity_product_facts(_row_nsn(), backend=backend)
    assert out["projected"]
    nsn_fact = next(f for f in out["facts"] if f["fact_type"] == "nsn")
    assert nsn_fact["evidence_source"] == "title+description"
    assert "4320-01-243-1951" in (nsn_fact["evidence_snippet"] or "")
    prod = backend.products[out["knowledge_product_id"]]
    ev = prod["specifications_json"]["nsn_evidence"]
    assert ev["source"] == "title+description"
    assert ev["snippet"]
    assert ev["timestamp"]
    assert prod["source"] == "fed_sam_contract_opportunities"


def test_intelligence_state_mapping():
    assert map_to_intelligence_state(ID_UNKNOWN) == INTEL_UNKNOWN
    assert map_to_intelligence_state(ID_EXACT_NSN) == INTEL_VALIDATED
    assert map_to_intelligence_state(READY_COMMERCIAL_RESEARCH) == "AVAILABLE"
    assert map_to_intelligence_state("PRODUCT_IDENTITY_PARTIAL") == "RESEARCH_REQUIRED"
    assert map_to_intelligence_state("DISCOVERED_ONLY") == INTEL_UNKNOWN


def test_pipeline_json_not_stripped():
    backend = InMemoryProjectionBackend()
    row = _row_nsn()
    original_struct = dict(row["dla_product_structure"])
    out = project_opportunity_product_facts(row, backend=backend)
    assert out["pipeline_json_preserved"] is True
    assert row["dla_product_structure"] == original_struct
    assert "product_identity" in row
