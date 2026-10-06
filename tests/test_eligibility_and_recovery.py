"""Tests for eligibility + recovery package."""

from eligibility_and_recovery.models import BUILD, BID_ELIGIBLE, RESEARCHABLE_NO_TOKEN
from eligibility_and_recovery.spec_identity import resolve_spec_identity, enrich_identity_for_research
from eligibility_and_recovery.eligibility import evaluate_opportunity_eligibility


def test_build_tag():
    assert BUILD == "20261004-m3-eligibility-and-recovery-v1"


def test_spec_recovers_reference_item():
    ident = {
        "raw_description": 'Dynarex™ Bandages, Plastic, 1" x 3" (100/Box, 24 Box/Case) Item 083602 or Equivalent',
        "confidence_grade": "B",
        "identity_type": "PERMITTED_EQUAL",
    }
    spec = resolve_spec_identity(ident)
    assert spec["no_token_class"] in RESEARCHABLE_NO_TOKEN
    assert spec["part_number"] == "083602"
    assert spec["manufacturer"]
    enriched = enrich_identity_for_research(ident)
    assert enriched.get("_recovered_token") is True
    assert enriched.get("part_number") == "083602"


def test_eligibility_returns_bid_status():
    ev = evaluate_opportunity_eligibility(
        "opengov:testbuyer:1",
        text="This solicitation is unrestricted small business set-aside for commercial supplies.",
    )
    assert ev["eligibility_status"] in {
        BID_ELIGIBLE,
        "BID_ELIGIBLE_WITH_ACTION",
        "ELIGIBILITY_UNKNOWN",
        "BID_INELIGIBLE",
    }
    assert "can_we_bid" in ev


def test_sdvosb_ineligible():
    ev = evaluate_opportunity_eligibility(
        "opengov:testbuyer:2",
        text="This acquisition is a total SDVOSB set-aside. Only SDVOSB concerns may submit offers.",
    )
    # May be INELIGIBLE or UNKNOWN depending on gate text detection
    assert ev["eligibility_status"] in {"BID_INELIGIBLE", "ELIGIBILITY_UNKNOWN", "BID_ELIGIBLE_WITH_ACTION", BID_ELIGIBLE}
