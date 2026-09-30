"""Unit tests for live opportunity novelty / product category classification."""

from __future__ import annotations

from scripts.run_m3_live_opportunity_test import classify_record


def test_classify_previously_seen_vs_new_product():
    known = {"sol:SPE7M123T0001:dla"}
    titles = set()
    seen = set()
    prev = classify_record(
        {
            "title": "NSN 1234 Pump Assembly",
            "agency": "DLA",
            "solicitation_number": "SPE7M123T0001",
            "status": "OPEN",
            "source_id": "fed_dla_dibbs_rfq",
            "deadline": "2099-12-31",
            "package_access": "PUBLIC",
        },
        known_keys=known,
        title_agency=titles,
        seen_this_run=seen,
    )
    assert prev["category"] == "E_PREVIOUSLY_SEEN_EXCLUDED"

    seen2: set[str] = set()
    fresh = classify_record(
        {
            "title": "Industrial Safety Gloves Qty 500",
            "agency": "Nebraska DOT",
            "solicitation_number": "NE-DOT-NEW-99999",
            "status": "OPEN",
            "source_id": "state_ne",
            "state_code": "NE",
            "deadline": "2099-12-31",
            "package_access": "PUBLIC",
            "product_classification": "CORE_PRODUCT",
        },
        known_keys=known,
        title_agency=titles,
        seen_this_run=seen2,
    )
    assert fresh["category"] in {
        "A_NEW_PRODUCT_ACTIONABLE",
        "B_NEW_PRODUCT_NEEDS_EVIDENCE",
        "D_NEW_AMBIGUOUS_CLASSIFICATION",
    }
    assert fresh["state"] == "NE"

    # Nevada BidNet must NOT be attributed to Nebraska (_ne substring trap)
    from scripts.run_m3_live_opportunity_test import _state_of

    assert _state_of({"source_id": "network_bidnet_nevada", "agency": "Nevada"}) == "UNK"
    assert _state_of({"source_id": "network_bidnet_nebraska", "agency": "Nebraska"}) == "NE"
    assert _state_of({"source_id": "network_bidnet_new_york", "agency": "New York"}) == "UNK"
    assert _state_of({"source_id": "network_bidnet_wyoming", "agency": "Wyoming"}) == "WY"

    dup = classify_record(
        {
            "title": "Industrial Safety Gloves Qty 500",
            "agency": "Nebraska DOT",
            "solicitation_number": "NE-DOT-NEW-99999",
            "status": "OPEN",
            "source_id": "portal_alt",
            "state_code": "NE",
        },
        known_keys=known,
        title_agency=titles,
        seen_this_run=seen2,
    )
    assert dup["category"] == "F_DUPLICATE_EXCLUDED"


def test_classify_service_and_expired_excluded():
    known: set[str] = set()
    titles: set[str] = set()
    seen: set[str] = set()
    svc = classify_record(
        {
            "title": "Janitorial Cleaning Services Annual Contract",
            "agency": "City Hall",
            "solicitation_number": "SVC-1001",
            "status": "OPEN",
            "source_id": "city_x",
            "product_classification": "SERVICE",
        },
        known_keys=known,
        title_agency=titles,
        seen_this_run=seen,
    )
    assert svc["category"] == "G_SERVICE_NON_CORE_EXCLUDED"

    seen2: set[str] = set()
    exp = classify_record(
        {
            "title": "Laptop Computers",
            "agency": "School District",
            "solicitation_number": "EXP-1001",
            "status": "EXPIRED",
            "source_id": "k12_x",
            "product_classification": "CORE_PRODUCT",
        },
        known_keys=known,
        title_agency=titles,
        seen_this_run=seen2,
    )
    assert exp["category"] == "H_EXPIRED_CANCELLED_EXCLUDED"
