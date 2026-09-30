"""BUILD 14 — COO / NMR / Set-Aside evidence binding tests."""

from __future__ import annotations

from m3_eligibility_evidence_read import (
    BUILD_TAG,
    ST_BLOCKED,
    ST_RESEARCH,
    ST_UNKNOWN,
    ST_VALIDATED,
    build_eligibility_evidence_profile,
    eligibility_to_research_items,
)
from m3_offer_readiness_read import build_offer_readiness_profile
from m3_research_queue import build_research_queue


def test_coo_requirement_extraction():
    row = {
        "canonical_id": "sol:elig1:dla",
        "title": "NSN 4320-01-243-1951",
        "description": "Buy American Act applies. Country of origin documentation required.",
    }
    profile = build_eligibility_evidence_profile(row)
    coo = profile["country_of_origin"]
    assert coo["status"] in {ST_RESEARCH, "DETECTED"}
    assert "Buy American" in coo["label"] or "origin" in coo["label"].lower()
    assert coo["evidence"]["extracted_text"]
    assert coo["evidence"]["source_document"]
    assert coo["evidence"]["timestamp"]
    assert coo["truth"]["does_not_infer_origin_from_supplier_location"] is True


def test_origin_evidence_binding_validated():
    row = {
        "canonical_id": "sol:elig2:dla",
        "description": "Trade Agreements Act (TAA) applies.",
        "product_origin": "United States",
        "origin_verified": True,
        "documents": [
            {
                "filename": "coo.pdf",
                "extracted_text": "Trade Agreements Act applies. Country of origin verified: United States.",
            }
        ],
    }
    profile = build_eligibility_evidence_profile(row)
    assert profile["country_of_origin"]["status"] == ST_VALIDATED
    assert "United States" in profile["country_of_origin"]["evidence"]["reason"]


def test_origin_not_inferred_from_supplier():
    row = {
        "canonical_id": "sol:elig3:dla",
        "description": "Buy American Act applies.",
        "supplier_product_graph": {
            "edges": [
                {
                    "supplier_name": "Ohio Widgets Inc",
                    "relationship_type": "DISTRIBUTOR",
                    "confidence": "HIGH",
                }
            ]
        },
    }
    profile = build_eligibility_evidence_profile(row)
    coo = profile["country_of_origin"]
    assert coo["status"] != ST_VALIDATED
    assert "Supplier location is not origin evidence" in coo["evidence"]["reason"]


def test_nmr_detection():
    row = {
        "canonical_id": "sol:elig4:dla",
        "set_aside": "Total Small Business",
        "description": "Nonmanufacturer Rule (NMR) may apply. NAICS 333911.",
        "naics": "333911",
    }
    profile = build_eligibility_evidence_profile(row)
    nmr = profile["nmr"]
    assert nmr["status"] in {ST_RESEARCH, "DETECTED"}
    assert nmr["truth"]["potentially_applicable_is_not_applicable"] is True
    assert nmr["truth"]["reseller_not_automatically_prohibited"] is True
    assert "applicable" in nmr["evidence"]["reason"].lower()
    # Must not claim NMR is definitively applicable
    assert "is applicable" not in nmr["evidence"]["reason"].lower() or "not a determination" in nmr["evidence"]["reason"].lower()


def test_nmr_waiver_evidence():
    row = {
        "canonical_id": "sol:elig5:dla",
        "set_aside": "Total Small Business",
        "documents": [
            {
                "filename": "amend1.pdf",
                "page": 2,
                "extracted_text": "SBA waiver of the Nonmanufacturer Rule (NMR waiver) is granted for this acquisition.",
            }
        ],
    }
    profile = build_eligibility_evidence_profile(row)
    assert profile["nmr"]["waiver_evidence"] is True
    assert profile["nmr"]["status"] == ST_VALIDATED
    assert "waiver" in profile["nmr"]["evidence"]["extracted_text"].lower()


def test_set_aside_detection():
    row = {
        "canonical_id": "sol:elig6:dla",
        "set_aside": "SDVOSB",
        "description": "This is a service-disabled veteran-owned small business set-aside.",
    }
    profile = build_eligibility_evidence_profile(row)
    sa = profile["set_aside"]
    assert sa["status"] == ST_RESEARCH
    assert "SDVOSB" in sa["label"] or "set-aside" in sa["label"].lower()
    assert sa["truth"]["set_aside_is_not_company_eligibility"] is True
    assert sa["status"] != ST_VALIDATED  # detection ≠ company eligible


def test_unknown_preservation():
    row = {"canonical_id": "sol:elig7:dla", "title": "Office chairs", "description": "Commercial items."}
    profile = build_eligibility_evidence_profile(row)
    assert profile["country_of_origin"]["status"] == ST_UNKNOWN
    assert profile["nmr"]["status"] == ST_UNKNOWN
    assert profile["set_aside"]["status"] == ST_UNKNOWN
    assert profile["unknown_preserved"] is True
    assert profile["rejects_on_missing_evidence"] is False


def test_false_eligibility_prevention():
    row = {
        "canonical_id": "sol:elig8:dla",
        "set_aside": "Total Small Business",
        "description": "Small business set-aside. Company is a reseller.",
        "company_facts": {"small_business": True},  # not enough alone for VALIDATED without set_aside_eligible
    }
    profile = build_eligibility_evidence_profile(row)
    assert profile["set_aside"]["status"] != ST_VALIDATED
    assert "does not mean the company is eligible" in profile["set_aside"]["evidence"]["reason"]


def test_offer_readiness_integration():
    row = {
        "canonical_id": "sol:elig9:dla",
        "agency": "DLA",
        "title": "Pump",
        "description": "Buy American Act. Total Small Business set-aside. NMR language present.",
        "set_aside": "Total Small Business",
        "product_identity": {"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        "dla_product_structure": {
            "has_exact_nsn": True,
            "set_aside_signal": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        "deal_economics": {"PRICE_CONFIDENCE": "HIGH"},
        "execution_intelligence": {"Financing_Fit": "SATISFIED"},
    }
    offer = build_offer_readiness_profile(row, research_items=[])
    cats = {
        r["category"]
        for r in offer["compliance"]["requirements"]
        if not r.get("scaffold") and r.get("source") == "eligibility_evidence"
    }
    assert "COUNTRY_OF_ORIGIN" in cats
    assert "NMR" in cats
    assert "SET_ASIDE" in cats


def test_research_queue_integration():
    row = {
        "canonical_id": "sol:elig10:dla",
        "description": "Buy American Act applies. SDVOSB set-aside.",
        "set_aside": "SDVOSB",
        "lifecycle": "RESEARCH",
    }
    profile = build_eligibility_evidence_profile(row)
    items = eligibility_to_research_items(profile, opportunity_id=row["canonical_id"])
    actions = " ".join(i["recommended_action"] for i in items).lower()
    assert "country of origin" in actions or "origin" in actions
    assert "size" in actions or "certification" in actions or "nmr" in actions

    q = build_research_queue(rows=[row], status_index={"by_key": {}}, limit=50)
    src_items = [i for i in q["items"] if i.get("source") == "eligibility_evidence"]
    assert src_items


def test_blocked_explicit_ineligibility():
    row = {
        "canonical_id": "sol:elig11:dla",
        "set_aside": "HUBZone",
        "company_facts": {"set_aside_eligible": False},
        "description": "HUBZone set-aside.",
    }
    profile = build_eligibility_evidence_profile(row)
    assert profile["set_aside"]["status"] == ST_BLOCKED


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-eligibility")
