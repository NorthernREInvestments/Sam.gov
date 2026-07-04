"""Tests for CSV opportunity dashboard cards and pursue action."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from csv_opportunity_service import csv_opportunity_to_card_dict, pursue_csv_opportunity
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
        status="New",
    )
    card = csv_opportunity_to_card_dict(row, today=date.today())
    assert card["notice_id"] == "abc-123"
    assert card["csv_opportunity"] is True
    assert card["naics_code"] == "561720"
    assert card["set_aside_display"].startswith("Total Small Business")
    assert card["workflow_progress"]["primary_action"]["action"] == "pursue_csv"
    assert card["days_until_due"] == 14


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
