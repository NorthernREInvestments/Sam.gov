"""Tests for USAspending offer-count extraction."""

from __future__ import annotations

from usaspending_client import (
    _extract_number_of_offers_from_detail,
    _parse_offer_count,
)


def test_parse_offer_count():
    assert _parse_offer_count("6") == 6
    assert _parse_offer_count(4) == 4
    assert _parse_offer_count("0") is None
    assert _parse_offer_count(None) is None
    assert _parse_offer_count("not-a-number") is None


def test_extract_number_of_offers_from_detail():
    detail = {
        "latest_transaction_contract_data": {
            "number_of_offers_received": "3",
        }
    }
    assert _extract_number_of_offers_from_detail(detail) == 3
    assert _extract_number_of_offers_from_detail({"number_of_offers_received": "2"}) == 2
    assert _extract_number_of_offers_from_detail({}) is None


def test_build_modification_history_labels_base_and_mods():
    from usaspending_client import build_modification_history

    history = build_modification_history(
        [
            {"modification_number": "0", "action_date": "2021-10-14", "federal_action_obligation": 693000},
            {"modification_number": "P00001", "action_date": "2022-02-07", "federal_action_obligation": 0, "action_type_description": "EXERCISE AN OPTION"},
        ]
    )
    assert history[0]["modification_number"] == "Base award"
    assert history[1]["modification_number"] == "P00001"
    assert history[1]["description"] == "EXERCISE AN OPTION"
