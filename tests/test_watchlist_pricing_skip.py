"""Tests for watchlist-sourced pricing and USAspending skip logic."""

from __future__ import annotations

from usaspending_savings import get_usaspending_savings, record_usaspending_skip, reset_usaspending_savings
from watchlist_pricing import apply_watchlist_pricing, pricing_from_watchlist, should_skip_usaspending_lookup


class _FakeContract:
    def __init__(self, **kwargs):
        self.notice_id = kwargs.get("notice_id", "TEST-1")
        self.pricing_intel = kwargs.get("pricing_intel")
        self.analysis = kwargs.get("analysis") or {}


def test_should_skip_only_for_high_or_confirmed():
    high = _FakeContract(
        analysis={
            "govspend_watchlist": {
                "match_confidence": "High",
                "match_score": 9,
                "estimated_annual_value": 500000,
                "award_amount": 480000,
                "incumbent_name": "Acme LLC",
                "contracting_office": "US Army ACC",
            }
        }
    )
    possible = _FakeContract(
        analysis={"govspend_watchlist": {"match_confidence": "Possible", "match_score": 6}}
    )
    confirmed = _FakeContract(
        analysis={
            "govspend_watchlist": {
                "match_confidence": "Possible",
                "match_confirmed": True,
                "award_amount": 100000,
            }
        }
    )
    assert should_skip_usaspending_lookup(high)
    assert not should_skip_usaspending_lookup(possible)
    assert should_skip_usaspending_lookup(confirmed)


def test_pricing_payload_uses_watchlist_fields():
    contract = _FakeContract(
        analysis={
            "govspend_watchlist": {
                "match_confidence": "High",
                "watchlist_id": 42,
                "estimated_annual_value": 750000,
                "award_amount": 700000,
                "incumbent_name": "Acme Facility Services",
                "contracting_office": "US Army Contracting Command",
            }
        }
    )
    payload = pricing_from_watchlist(contract)
    assert payload is not None
    assert payload["prior_contract_annual"] == 750000
    assert payload["prior_contract_total"] == 700000
    assert payload["contract_value"] == 700000
    assert payload["awarding_office"] == "US Army Contracting Command"
    assert payload["likely_incumbent"] == "Acme Facility Services"
    pred = payload["predecessor_award"]
    assert pred["historical_award_amount"] == 700000
    assert pred["annual_amount"] == 750000
    assert pred["amount_basis"] == "annual"
    assert pred["recipient_name"] == "Acme Facility Services"
    assert payload["skip_usaspending"] is True


def test_apply_records_usaspending_savings():
    reset_usaspending_savings()
    contract = _FakeContract(
        notice_id="N-100",
        analysis={
            "govspend_watchlist": {
                "match_confidence": "High",
                "estimated_annual_value": 100000,
                "award_amount": 95000,
                "incumbent_name": "Incumbent Co",
            }
        },
    )
    assert apply_watchlist_pricing(contract)
    savings = get_usaspending_savings()
    assert savings["usaspending_skipped_contracts"] == 1
    assert savings["usaspending_calls_saved"] == 2
    assert not record_usaspending_skip(contract)
