"""Unit tests for watchlist fingerprint scoring."""

from __future__ import annotations

from gs_watchlist_service import WatchlistTarget, score_contract_against_target
from watchlist_fingerprint import (
    confidence_from_score,
    extract_title_keywords,
    score_fingerprint_match,
    values_within_tolerance,
)


class _FakeContract:
    def __init__(self, **kwargs):
        self.notice_id = kwargs.get("notice_id", "TEST-1")
        self.title = kwargs.get("title")
        self.agency = kwargs.get("agency")
        self.location = kwargs.get("location")
        self.naics_code = kwargs.get("naics_code")
        self.estimated_value = kwargs.get("estimated_value")
        self.description = kwargs.get("description")
        self.link = kwargs.get("link")
        self.due_date = None
        self.sam_raw = kwargs.get("sam_raw") or {}
        self.analysis = kwargs.get("analysis") or {}
        self.pricing_intel = kwargs.get("pricing_intel")


def _target(**overrides) -> WatchlistTarget:
    base = dict(
        id=1,
        award_id="AWARD-1",
        contract_name="Fort Bragg Grounds Maintenance Services Contract",
        agency="Department of the Army",
        contracting_office="US Army Contracting Command",
        location_city="Fort Liberty",
        location_state="NC",
        location_zip="28310",
        naics_code="561730",
        incumbent_name="Acme Facility Services LLC",
        award_amount=1_000_000.0,
        estimated_annual_value=1_000_000.0,
        title_keywords=tuple(extract_title_keywords("Fort Bragg Grounds Maintenance Services Contract")),
        priority="High",
        status="Watching",
    )
    base.update(overrides)
    return WatchlistTarget(**base)


def test_confidence_tiers():
    assert confidence_from_score(8) == "High"
    assert confidence_from_score(11) == "High"
    assert confidence_from_score(7) == "Possible"
    assert confidence_from_score(5) == "Possible"
    assert confidence_from_score(4) == "Weak"
    assert confidence_from_score(3) == "Weak"
    assert confidence_from_score(2) == "None"


def test_values_within_thirty_percent():
    assert values_within_tolerance(1_000_000, 850_000)
    assert values_within_tolerance(1_000_000, 1_200_000)
    assert not values_within_tolerance(1_000_000, 600_000)


def test_high_confidence_match():
    target = _target()
    contract = _FakeContract(
        title="Fort Bragg Grounds Maintenance and Landscaping Services",
        agency="US Army Contracting Command, ACC-RDU",
        location="Fort Liberty, NC 28310",
        naics_code="561730",
        estimated_value="$950,000",
        description="Incumbent Acme Facility Services LLC currently performs grounds maintenance.",
        sam_raw={"fullParentPathName": "DEPT OF DEFENSE.DEPT OF THE ARMY.US ARMY CONTRACTING COMMAND"},
    )
    result = score_contract_against_target(contract, target)
    assert result.score >= 8
    assert result.confidence == "High"
    assert "incumbent_name" in result.matched_signals


def test_possible_match_without_incumbent():
    target = _target(estimated_annual_value=2_500_000.0, award_amount=2_500_000.0)
    contract = _FakeContract(
        title="Fort Bragg Grounds Maintenance Services",
        agency="US Army Contracting Command",
        location="Fort Liberty, NC 28310",
        naics_code="561730",
        estimated_value="$950,000",
        description="Grounds maintenance at Fort Liberty.",
    )
    result = score_contract_against_target(contract, target)
    assert 5 <= result.score <= 7
    assert result.confidence == "Possible"


def test_weak_match_logged_only():
    target = _target()
    contract = _FakeContract(
        title="Unrelated janitorial services",
        agency="US Army Contracting Command",
        location="Fort Liberty, NC",
        naics_code="561730",
    )
    from watchlist_fingerprint import posting_fingerprint_from_contract

    posting = posting_fingerprint_from_contract(contract)
    result = score_fingerprint_match(posting, target)
    assert result.confidence in ("Weak", "None")
