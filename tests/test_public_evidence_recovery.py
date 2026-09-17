"""Tests for public evidence recovery, deadline conflict, reconstructed vs authoritative."""

from __future__ import annotations

from deadline_conflict import (
    deadline_evidence_record,
    extract_open_close_from_sciquest_text,
    reconcile_deadline_evidence,
)
from portal_registration_decision import assess_portal_registration
from preliminary_economics import compute_evidence_bounded_economics
from public_evidence_constants import (
    DEADLINE_CONFLICT_UNRESOLVED,
    DEADLINE_OPEN_VS_CLOSE_CLARIFIED,
    DEADLINE_STALE_MIRROR,
    EV_AUTHORITATIVE_CURRENT,
    EV_HISTORICAL_AWARD,
    EV_MANUFACTURER,
    EV_THIRD_PARTY_MIRROR,
    MATCH_POSSIBLE,
    PORTAL_REQUIRED,
    PRICE_HISTORICAL_GOV_UNIT,
)
from public_evidence_models import (
    agency_item_intelligence,
    assert_reconstructed_not_merged_into_requirement,
    empty_reconstructed_specification,
    set_reconstructed_field,
)
from public_evidence_recovery import classify_commercial_match, build_reconstructed_spec_from_public


SCIQUEST_SNIP = """
Open
9/15/2026, 1:00 PM CDT
Type
DOT Request for Bid
Close
10/7/2026, 1:00 PM CDT
Number
645-DOTRFB-2975-2027
Sealed Until
10/7/2026, 1:00 PM CDT
Tungsten-Carbide BLADE 3 FT. SECTION (rubber encased)
"""


def test_open_close_extraction_and_mirror_confusion():
    auth = extract_open_close_from_sciquest_text(SCIQUEST_SNIP, document="event.pdf")
    assert any(r["role"] == "OPEN" and "9/15" in r["value"] for r in auth)
    assert any(r["role"] == "CLOSE" and "10/7" in r["value"] for r in auth)
    mirror = deadline_evidence_record(
        raw="9/15/2026, 1:00 PM CDT",
        role="MIRROR_CLAIMED_DEADLINE",
        evidence_class=EV_THIRD_PARTY_MIRROR,
        source_url="https://mirror.example/bid",
    )
    result = reconcile_deadline_evidence(auth + [mirror])
    assert result["status"] == DEADLINE_OPEN_VS_CLOSE_CLARIFIED
    assert "10/7" in (result["best_supported_close"] or "")
    assert result["conflict_resolved"] is True


def test_unresolved_conflict_uses_earliest_operational():
    a = deadline_evidence_record(
        raw="10/7/2026, 1:00 PM CDT",
        role="CLOSE",
        evidence_class=EV_THIRD_PARTY_MIRROR,
    )
    b = deadline_evidence_record(
        raw="9/15/2026, 1:00 PM CDT",
        role="CLOSE",
        evidence_class=EV_THIRD_PARTY_MIRROR,
    )
    result = reconcile_deadline_evidence([a, b])
    assert result["status"] == DEADLINE_CONFLICT_UNRESOLVED
    assert result["conflict_resolved"] is False
    assert "9/15" in (result["operational_deadline"] or "")


def test_authoritative_close_over_stale_mirrors():
    auth = deadline_evidence_record(
        raw="10/7/2026, 1:00 PM CDT",
        role="CLOSE",
        evidence_class=EV_AUTHORITATIVE_CURRENT,
    )
    mirrors = [
        deadline_evidence_record(
            raw="9/15/2026, 1:00 PM CDT",
            role="MIRROR_CLAIMED_DEADLINE",
            evidence_class=EV_THIRD_PARTY_MIRROR,
        ),
        deadline_evidence_record(
            raw="9/17/2026",
            role="MIRROR_CLAIMED_DEADLINE",
            evidence_class=EV_THIRD_PARTY_MIRROR,
        ),
    ]
    result = reconcile_deadline_evidence([auth] + mirrors)
    assert result["status"] in {DEADLINE_STALE_MIRROR, DEADLINE_OPEN_VS_CLOSE_CLARIFIED, "DEADLINE_RESOLVED"}
    assert "10/7" in (result["operational_deadline"] or result["best_supported_close"] or "")


def test_reconstructed_not_authoritative_and_not_merged():
    recon = build_reconstructed_spec_from_public(
        authoritative_text=SCIQUEST_SNIP,
        commercial_texts=[
            {
                "text": "Winter Equipment carbide snow plow blade width: 6 inches",
                "url": "https://www.winterequipment.com/",
                "document": "winter",
                "evidence_class": EV_MANUFACTURER,
            }
        ],
    )
    assert recon["authoritative_for_current_bid"] is False
    assert all(f.get("authoritative_for_bidding") is False for f in recon["fields"].values())
    assert all(f.get("authoritative_for_current_bid") is False for f in recon["fields"].values())

    requirement = {
        "terms": {
            "bid_deadline": {
                "value": "10/7/2026, 1:00 PM CDT",
                "confidence": "VERIFIED_DOCUMENT",
                "source_document": "event.pdf",
            }
        }
    }
    assert assert_reconstructed_not_merged_into_requirement(requirement, recon) is True

    # Historical cannot overwrite as authoritative bidding fact via reconstructed path
    set_reconstructed_field(
        recon,
        "width",
        "99 inches invented",
        evidence_class=EV_HISTORICAL_AWARD,
        confidence="LOW",
        historical_or_current="historical",
    )
    assert recon["fields"]["width"]["authoritative_for_current_bid"] is False


def test_item_code_intelligence_and_price_dates():
    row = agency_item_intelligence(
        agency_item_code="002367300",
        historical_description="Tungsten-Carbide BLADE 3 FT",
        historical_unit_price=85.0,
        award_date="2024-01-15",
        evidence_class=EV_HISTORICAL_AWARD,
        confidence="LOW",
        authoritative_for_current_bid=True,  # should be forced false
    )
    assert row["authoritative_for_current_bid"] is False
    assert row["price_date"] == "2024-01-15"
    assert row["historical_unit_price"] == 85.0


def test_no_false_compliance_claim():
    state = classify_commercial_match(
        {"description": "carbide snow plow blade", "dimensions": "3ft"},
        {"carbide": "tungsten"},
    )
    assert state in {MATCH_POSSIBLE, "STRONG_SPEC_MATCH", "QUOTE_REQUIRED"}
    product = {
        "manufacturer": "Winter Equipment",
        "fit_state": state,
        "compliant_with_current_solicitation": False,
    }
    assert product["compliant_with_current_solicitation"] is False


def test_evidence_bounded_economics_and_unknown():
    empty = compute_evidence_bounded_economics(total_quantity=900)
    assert empty["status"] == "UNKNOWN"
    assert empty["winning_price_claimed"] is False

    econ = compute_evidence_bounded_economics(
        total_quantity=900,
        unit_price_evidence=[
            {
                "unit_price": 40.0,
                "price_class": PRICE_HISTORICAL_GOV_UNIT,
                "date": "2023-11-01",
                "source_url": "https://example.gov/tab",
            },
            {
                "unit_price": 55.0,
                "price_class": PRICE_HISTORICAL_GOV_UNIT,
                "date": "2024-02-01",
                "source_url": "https://example.gov/tab2",
            },
        ],
    )
    assert econ["status"] == "EVIDENCE_BOUNDED"
    assert econ["LOW_EVIDENCE_COST_CASE"]["extended_supplier_cost"] == 36000.0
    assert econ["HIGH_EVIDENCE_COST_CASE"]["extended_supplier_cost"] == 49500.0
    assert econ["preliminary_landed_range"] is None  # freight unknown ≠ 0
    assert econ["LOW_EVIDENCE_COST_CASE"]["is_current_supplier_cost"] is False
    assert econ["minimum_bid_for_10k_profit_range"]["low"] == 46000.0


def test_portal_registration_states():
    r = assess_portal_registration(
        auth_blocked_critical_doc=True,
        authoritative_spec_available=False,
        product_id_state="BRAND_OR_EQUAL_IDENTIFIED",
        economics={"status": "UNKNOWN"},
        bid_submission_requires_registration=True,
    )
    assert r["state"] == PORTAL_REQUIRED
    assert r["autonomous_registration"] is False
    assert r["agency_outreach"] == 0
    assert r["supplier_outreach"] == 0
    assert r["lender_outreach"] == 0
    assert r["bid_submissions"] == 0


def test_empty_reconstructed_spec_structure():
    spec = empty_reconstructed_specification()
    assert spec["kind"] == "ReconstructedSpecification"
    assert "hole_pattern" in spec["fields"]
