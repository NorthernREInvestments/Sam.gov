"""Tests for CSV opportunity dashboard cards and pursue action."""

from __future__ import annotations

from datetime import date, timedelta

from csv_opportunity_service import (
    _days_bucket,
    _matches_filters,
    _sort_csv_cards,
    csv_opportunity_to_card_dict,
    pursue_csv_opportunity,
)
from models import CsvOpportunity


def test_csv_opportunity_card_dict_fields():
    row = CsvOpportunity(
        notice_id="abc-123",
        title="Janitorial Services",
        agency="VETERANS AFFAIRS",
        location_city="Denver",
        location_state="CO",
        due_date=date.today() + timedelta(days=14),
        naics_code="561720",
        set_aside="Total Small Business Set-Aside (FAR 19.5)",
        co_name="Jane Doe",
        co_email="jane@va.gov",
        sam_url="https://sam.gov/opp/abc-123/view",
        watchlist_match_confidence="High",
        watchlist_match_score=9,
        status="New",
    )
    card = csv_opportunity_to_card_dict(row, today=date.today())
    assert card["notice_id"] == "abc-123"
    assert card["co_name"] == "Jane Doe"
    assert card["co_email"] == "jane@va.gov"
    assert card["watchlist_match_confidence"] == "High"
    assert card["workflow_progress"]["primary_action"]["action"] == "pursue_csv"
    assert card["days_until_due"] == 14


def test_days_bucket_and_filters():
    assert _days_bucket(3) == "under_7"
    assert _days_bucket(10) == "7_14"
    assert _days_bucket(20) == "14_30"
    assert _days_bucket(45) == "30_plus"
    card = {
        "title": "Landscaping at Fort Hood",
        "location_state": "TX",
        "naics_code": "561730",
        "days_until_due": 5,
    }
    assert _matches_filters(card, state="TX", days_bucket="under_7", naics_code="561730", keyword="landscaping")
    assert not _matches_filters(card, state="CO", days_bucket=None, naics_code=None, keyword=None)


def test_sort_pursuing_to_top():
    cards = [
        {"notice_id": "b", "csv_status": "New", "due_date": "2026-07-10", "days_until_due": 6},
        {
            "notice_id": "a",
            "csv_status": "Pursuing",
            "due_date": "2026-08-01",
            "pursued_at": "2026-07-04T12:00:00+00:00",
        },
    ]
    sorted_cards = _sort_csv_cards(cards)
    assert sorted_cards[0]["notice_id"] == "a"


def test_pursue_csv_opportunity_not_found():
    class FakeSession:
        def query(self, model):
            return self

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    result = pursue_csv_opportunity(FakeSession(), "missing")
    assert result == {"ok": False, "error": "not_found"}
