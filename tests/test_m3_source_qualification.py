"""BUILD 13 — QPL / Source Approval Research Lane tests."""

from __future__ import annotations

from m3_offer_readiness_read import build_offer_readiness_profile
from m3_source_qualification_read import (
    APPROVED_SOURCE_REQUIRED,
    BUILD_TAG,
    QPL_REQUIRED,
    ST_BLOCKED,
    ST_DETECTED,
    ST_RESEARCH,
    ST_UNKNOWN,
    ST_VALIDATED,
    build_source_qualification_profile,
    qualification_to_research_items,
)


def test_approved_source_detected():
    row = {
        "canonical_id": "sol:qpl1:dla",
        "title": "NSN 4320-01-243-1951 PUMP",
        "description": "Approved source required. QPL applies.",
        "dla_product_structure": {
            "nsn": "4320-01-243-1951",
            "approved_source_signal": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
    }
    profile = build_source_qualification_profile(row)
    types = {r["qualification_type"] for r in profile["requirements"]}
    assert APPROVED_SOURCE_REQUIRED in types or QPL_REQUIRED in types
    assert profile["status"] in {ST_DETECTED, ST_RESEARCH, ST_BLOCKED}
    assert profile["assumes_qualification"] is False


def test_unsupported_supplier_not_validated():
    row = {
        "canonical_id": "sol:qpl2:dla",
        "title": "NSN 4320-01-243-1951",
        "description": "Approved source required.",
        "dla_product_structure": {"approved_source_signal": True, "nsn": "4320-01-243-1951"},
        "supplier_product_graph": {
            "edges": [
                {
                    "supplier_name": "ABC Manufacturing",
                    "relationship_type": "HISTORICAL_GOVERNMENT_SUPPLIER",
                    "confidence": "VALIDATED",
                    "source": "award_history",
                }
            ]
        },
    }
    profile = build_source_qualification_profile(row)
    assert profile["validated_supplier_count"] == 0
    abc = next(s for s in profile["suppliers"] if "ABC" in s["supplier_name"])
    assert abc["status"] == ST_UNKNOWN
    assert "not QPL" in abc["evidence"]["evidence"] or "not" in abc["evidence"]["evidence"].lower()
    assert profile["status"] != ST_VALIDATED


def test_listed_supplier_validated_with_evidence():
    row = {
        "canonical_id": "sol:qpl3:dla",
        "title": "NSN 4320-01-243-1951",
        "description": "Approved source: ABC Manufacturing for this NSN.",
        "dla_product_structure": {
            "approved_source_signal": True,
            "nsn": "4320-01-243-1951",
            "fields": {"nsn": {"value": "4320-01-243-1951"}},
        },
        "supplier_product_graph": {
            "edges": [
                {
                    "supplier_name": "ABC Manufacturing",
                    "relationship_type": "HISTORICAL_GOVERNMENT_SUPPLIER",
                    "confidence": "HIGH",
                }
            ]
        },
    }
    profile = build_source_qualification_profile(row)
    assert profile["status"] == ST_VALIDATED
    assert profile["validated_supplier_count"] >= 1
    assert profile["offer_readiness"]["status"] == ST_VALIDATED
    ev = profile["suppliers"][0]["evidence"]
    assert ev["source_document"]
    assert ev["requirement_text"]
    assert ev["product"]
    assert ev["supplier"]
    assert ev["evidence"]
    assert ev["confidence"]
    assert ev["timestamp"]


def test_qpl_requirement_creates_research_item():
    row = {
        "canonical_id": "sol:qpl4:dla",
        "description": "Item must be on the Qualified Products List (QPL).",
        "dla_product_structure": {"nsn": "1234-01-234-5678"},
    }
    profile = build_source_qualification_profile(row)
    assert any(r["qualification_type"] == QPL_REQUIRED for r in profile["requirements"])
    items = qualification_to_research_items(profile, opportunity_id=row["canonical_id"])
    assert items
    actions = " ".join(i["recommended_action"] for i in items).lower()
    assert "qpl" in actions


def test_no_eligible_source_blocked():
    row = {
        "canonical_id": "sol:qpl5:dla",
        "description": "Approved source required. Source controlled. No substitutions.",
        "dla_product_structure": {"approved_source_signal": True, "nsn": "4320-01-243-1951"},
        # no suppliers
    }
    profile = build_source_qualification_profile(row)
    assert profile["status"] == ST_BLOCKED
    assert profile["offer_readiness"]["status"] == ST_BLOCKED
    assert "no eligible" in profile["offer_readiness"]["label"].lower() or profile["offer_readiness"]["blocker_type"] == "HARD_BLOCKER"


def test_offer_readiness_updates():
    row = {
        "canonical_id": "sol:qpl6:dla",
        "agency": "DLA",
        "title": "Pump",
        "description": "Approved source required.",
        "product_identity": {"identity_state": "EXACT_NSN", "confidence": "HIGH"},
        "dla_product_structure": {
            "has_exact_nsn": True,
            "approved_source_signal": True,
            "fields": {"nsn": {"value": "4320-01-243-1951", "confidence": "HIGH"}},
        },
        "deal_economics": {"PRICE_CONFIDENCE": "HIGH"},
        "execution_intelligence": {"Financing_Fit": "SATISFIED"},
    }
    profile = build_offer_readiness_profile(row, research_items=[])
    src = [
        r
        for r in profile["compliance"]["requirements"]
        if r.get("category") == "SOURCE_APPROVAL" and not r.get("scaffold")
    ]
    assert src
    assert src[0].get("source") == "source_qualification"
    assert src[0].get("status") in {ST_RESEARCH, ST_BLOCKED, ST_DETECTED}


def test_supplier_graph_unchanged_and_no_false_approvals():
    from m3_supplier_product_graph import BUILD_TAG as SPG_TAG, build_supplier_product_graph_view

    row = {
        "canonical_id": "sol:qpl7:dla",
        "description": "Website sells similar product from XYZ Corp.",
        "dla_product_structure": {"approved_source_signal": True},
        "supplier_product_graph": {
            "edges": [
                {
                    "edge_id": "e1",
                    "supplier_name": "XYZ Corp",
                    "relationship_type": "DISTRIBUTOR",
                    "confidence": "POSSIBLE",
                    "product_dedupe_key": "nsn:1",
                }
            ]
        },
    }
    before = build_supplier_product_graph_view(rows=[row], limit=10)
    profile = build_source_qualification_profile(row)
    after = build_supplier_product_graph_view(rows=[row], limit=10)
    assert before["count"] == after["count"]
    assert profile["supplier_graph_unchanged"] is True
    assert profile["false_approvals_forbidden"] is True
    assert all(s["status"] != ST_VALIDATED for s in profile["suppliers"])
    assert SPG_TAG  # module still importable / unchanged contract


def test_build_pin():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION
    assert BUILD_TAG.startswith("20260918-m3-source-qualification")
