"""Tests for eligibility applicability + independent price routes."""

from __future__ import annotations

from eligibility_and_recovery.applicability import (
    BUILD,
    audit_requirement,
    classify_bonding,
    classify_insurance,
    classify_site_visit,
    recompute_opportunity,
)
from public_price_search.circuits import is_open, note, reset_all, status_of
from public_price_search.search import serp_circuit_open


def test_build_target():
    assert BUILD == "20261004-m3-price-search-reliability-v1"


def test_bonding_soft_vs_mandatory():
    soft = classify_bonding("Bonds may be required at the option of the buyer.")
    assert soft["bonding_class"] in {"OPTIONAL", "REFERENCE_ONLY"}
    assert soft["creates_action"] is False

    hard = classify_bonding("A bid bond of 5% must accompany the bid.")
    assert hard["bonding_class"] == "BID_BOND_REQUIRED"
    assert hard["creates_action"] is True


def test_insurance_post_award_not_action():
    post = classify_insurance(
        "Contractor shall maintain insurance and provide a certificate of insurance prior to commencement of work."
    )
    assert post["insurance_class"] == "REQUIRED_AFTER_AWARD"
    assert post["creates_action"] is False


def test_site_visit_mandatory_only():
    opt = classify_site_visit("An optional pre-bid conference will be held.")
    assert opt["creates_action"] is False
    mand = classify_site_visit("Mandatory site visit — attendance is mandatory or bid will be rejected.")
    assert mand["site_visit_class"] == "MANDATORY_PREBID"
    assert mand["creates_action"] is True


def test_buy_american_school_lunch_not_applicable():
    hit = audit_requirement(
        {
            "blocking_requirement": "BUY_AMERICAN",
            "exact_requirement": (
                "CERTIFICATION OF COMPLIANCE WITH BUY AMERICAN PROVISIONS "
                "[Only Applicable to Contracts funded under the National School Lunch]"
            ),
        }
    )
    assert hit["applicability"] == "NOT_APPLICABLE"
    assert hit["creates_action"] is False


def test_berry_clause_presence_not_auto_kill():
    hit = audit_requirement(
        {
            "blocking_requirement": "DFARS 252.225-7012",
            "exact_requirement": "DFARS 252.225-7012 Preference for Certain Domestic Commodities (see clause matrix)",
            "clause": "DFARS 252.225-7012",
        },
        kind="far",
    )
    assert hit.get("eligibility_blocker") is False
    assert hit.get("far_status") in {"NEEDS_REVIEW", "DOES_NOT_APPLY"}


def test_recompute_drops_false_positive_actions():
    ev = {
        "eligibility_status": "BID_ELIGIBLE_WITH_ACTION",
        "file_coverage": {"documents_total": 3, "documents_processed": 3},
        "fatal_blockers": [],
        "actionable_blockers": [
            {
                "blocking_requirement": "INSURANCE_COI",
                "exact_requirement": "Contractor shall maintain insurance after award.",
            },
            {
                "blocking_requirement": "W9_SUBMISSION",
                "exact_requirement": "Form W-9 (Rev. March 2024) Request for Taxpayer Identification Number",
            },
            {
                "blocking_requirement": "BONDING",
                "exact_requirement": "Performance bonds may be required.",
            },
        ],
        "far_dfars": [],
    }
    out = recompute_opportunity(ev)
    assert out["eligibility_status"] == "BID_ELIGIBLE"
    assert len(out["false_positive_actions_removed"]) >= 2


def test_provider_circuits_independent():
    reset_all()
    assert serp_circuit_open() is False
    for _ in range(3):
        note("SEARCH_PROVIDER_A", ok=False, reason="NO_RESULTS")
    assert is_open("SEARCH_PROVIDER_A") is True
    assert is_open("SEARCH_PROVIDER_B") is False
    assert serp_circuit_open() is False  # B still healthy → not global kill
    for _ in range(3):
        note("SEARCH_PROVIDER_B", ok=False, reason="NO_RESULTS")
    assert serp_circuit_open() is True
    reset_all()
    assert status_of("MANUFACTURER_SEARCH") == "HEALTHY"


def test_catalog_circuit_is_domain_scoped_only():
    reset_all()
    for _ in range(5):
        note("DIRECT_CATALOG", ok=False, domain="blocked.example", reason="403")
    assert is_open("DIRECT_CATALOG", domain="blocked.example") is True
    assert is_open("DIRECT_CATALOG", domain="ok.example") is False
    assert is_open("DIRECT_CATALOG") is False  # family never globally open
    reset_all()
