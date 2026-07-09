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


def test_csv_pricing_display_prior_offer_count_overrides_regional():
    intel = {
        "predecessor_award": {
            "is_prior_contract": True,
            "annual_amount": 340700,
            "recipient_name": "ASHLEY-MARIE GROUP, INC.",
            "lookup_method": "contract_number",
            "number_of_offers_received": 6,
        },
        "unique_bidders": 12,
    }
    display = csv_pricing_card_display(intel)
    assert display["unique_bidders"] == 6


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


def test_csv_pricing_display_pending_when_not_run():
    display = csv_pricing_card_display(None)
    assert display["kind"] == "pending"
    assert "not run" in display["main_line"].lower()


def test_csv_pricing_display_shows_specific_error():
    display = csv_pricing_card_display({"error": "Work state missing"})
    assert display["kind"] == "error"
    assert display["main_line"] == "Work state missing"


def test_csv_row_needs_pricing_skips_cached():
    from csv_pricing_service import csv_row_needs_pricing

    row = type("Row", (), {"pricing_intel": {"cached_at": "2026-07-04", "tier": "csv_usaspending"}})()
    assert not csv_row_needs_pricing(row)
    assert csv_row_needs_pricing(row, force=True)
