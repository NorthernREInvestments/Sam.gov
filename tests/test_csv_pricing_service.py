"""Tests for CSV opportunity USAspending pricing display."""

from __future__ import annotations

from csv_pricing_service import csv_pricing_card_display


def test_csv_pricing_display_prior_same_location():
    intel = {
        "predecessor_award": {
            "is_prior_contract": True,
            "recent_annual_amount": 125000,
            "recipient_name": "Acme Janitorial LLC",
            "lookup_method": "agency_facility_match",
        },
        "unique_bidders": 4,
    }
    display = csv_pricing_card_display(intel)
    assert display["kind"] == "prior"
    assert "Prior:" in display["main_line"]
    assert "Acme" in display["main_line"]
    assert display["source_label"] == "Same location"
    assert display["unique_bidders"] == 4


def test_csv_pricing_display_regional_average():
    intel = {
        "average_annual_award": 98000,
        "awards_count": 12,
        "unique_bidders": 7,
    }
    display = csv_pricing_card_display(intel)
    assert display["kind"] == "regional"
    assert display["main_line"].startswith("Regional avg:")
    assert display["source_label"] == "Regional estimate"
    assert display["unique_bidders"] == 7
