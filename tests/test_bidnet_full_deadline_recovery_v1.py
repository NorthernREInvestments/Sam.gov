"""Tests for BidNet full deadline recovery identity + ingest."""

from __future__ import annotations

from buyer_intelligence.bidnet_full_deadline_recovery_v1 import (
    index_store,
    ingest_or_patch_list_item,
    match_list_item,
)


def test_match_list_item_by_solicitation_id() -> None:
    store = {
        "abc123": {
            "authoritative_url": (
                "https://www.bidnetdirect.com/public/supplier/solicitations/"
                "statewide/444176375001/abstract"
            ),
            "solicitation_event_id": "444176375001",
        }
    }
    idx = index_store(store)
    item = {
        "solicitation_id": "444176375001",
        "authoritative_url": "https://www.bidnetdirect.com/pa/solicitations/open-bids/foo",
        "deadline_raw": "10/30/2026",
    }
    hit = match_list_item(item, idx)
    assert hit is not None
    assert hit[0] == "abc123"
    assert hit[1] == "EXACT_SOURCE_ID"


def test_ingest_new_open_notice_when_unmatched() -> None:
    store: dict = {}
    idx = index_store(store)
    item = {
        "title": "Office Supplies Kit",
        "agency": "Pennsylvania",
        "location": "Pennsylvania",
        "solicitation_id": "444199999001",
        "authoritative_url": "https://www.bidnetdirect.com/pennsylvania/solicitations/open-bids/Office-Supplies",
        "deadline_raw": "11/15/2026",
    }
    row = ingest_or_patch_list_item(store, idx, item, slug="statewide-public", page=1)
    assert row is not None
    assert row["action"] == "INGEST_NEW"
    assert row["deadline_raw"] == "11/15/2026"
    oid = row["opportunity_id"]
    assert oid in store
    assert store[oid].get("deadline")
    assert store[oid].get("deadline_raw") == "11/15/2026"


def test_patch_existing_null_deadline_idempotent() -> None:
    store = {
        "abc123": {
            "canonical_opportunity_id": "abc123",
            "title": "Existing",
            "authoritative_url": (
                "https://www.bidnetdirect.com/public/supplier/solicitations/"
                "statewide/444176375001/abstract"
            ),
            "solicitation_event_id": "444176375001",
            "deadline": None,
        }
    }
    idx = index_store(store)
    item = {
        "title": "Existing",
        "solicitation_id": "444176375001",
        "authoritative_url": "https://www.bidnetdirect.com/x/solicitations/open-bids/Existing",
        "deadline_raw": "12/01/2026",
    }
    row = ingest_or_patch_list_item(store, idx, item, slug="statewide-public", page=2)
    assert row is not None
    assert row["action"] == "PATCH_EXISTING"
    assert store["abc123"].get("deadline")
    # Second apply should no-op (already has deadline)
    row2 = ingest_or_patch_list_item(store, idx, item, slug="statewide-public", page=3)
    assert row2 is None
