"""Deadline extraction repair regressions."""

from __future__ import annotations

from buyer_intelligence.available_deals_visibility_v1 import NEEDS_VERIFICATION, WORTH, classify_record
from buyer_intelligence.deadline_extraction_repair_v1 import (
    CLOSED_CONFIRMED,
    DEADLINE_UNKNOWN,
    OPEN_CONFIRMED,
    apply_candidate,
    classify_deadline_status,
    extract_title_closing,
    promote_deadline_from_record,
)


def test_title_closing_extraction():
    rec = {"title": "RFB-IS-27200181 - ASUS Chromebox - Closing: 10/19/2026 12:00 PM"}
    hit = extract_title_closing(rec)
    assert hit is not None
    assert "2026-10-19" in str(hit.get("normalized") or "")


def test_apply_does_not_overwrite_existing():
    rec = {"deadline": "2026-11-01T00:00:00+00:00", "title": "Keep me"}
    result = apply_candidate(
        rec,
        deadline="2026-12-01T00:00:00+00:00",
        deadline_raw="12/01/2026",
        source="test",
    )
    assert result["applied"] is False
    assert rec["deadline"].startswith("2026-11-01")


def test_apply_fills_missing():
    rec = {"title": "Fill me", "row_ref": {}}
    result = apply_candidate(
        rec,
        deadline="2026-11-12T20:30:00+00:00",
        deadline_raw="11/12/2026 04:30 PM EDT",
        timezone_name="America/New_York",
        source="test",
    )
    assert result["applied"] is True
    assert rec["deadline"].startswith("2026-11-12")
    assert rec["deadline_raw"]


def test_promote_response_deadline_alias():
    row = {"responseDeadLine": "2026-11-12T16:30:00-04:00"}
    got = promote_deadline_from_record(row)
    assert got is not None
    assert "2026-11-12" in str(got)


def test_unknown_deadline_not_expired_bucket():
    rec = {
        "title": "Paper towels supply",
        "set_aside": "Unrestricted",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "platform": "live_bidnet",
        "is_federal": False,
        "jurisdiction": "STATE_LOCAL",
    }
    assert classify_deadline_status(rec) == DEADLINE_UNKNOWN
    row = classify_record("oid-unk", rec)
    assert row["visibility_bucket"] == NEEDS_VERIFICATION


def test_future_deadline_worth_researching():
    rec = {
        "title": "Paper towels supply",
        "set_aside": "Unrestricted",
        "product_service_classification": "TANGIBLE_PRODUCT",
        "platform": "live_bidnet",
        "is_federal": False,
        "jurisdiction": "STATE_LOCAL",
        "deadline": "2099-12-01T00:00:00+00:00",
        "deadline_status": OPEN_CONFIRMED,
    }
    row = classify_record("oid-open", rec)
    assert row["visibility_bucket"] == WORTH
    assert classify_deadline_status(rec) == OPEN_CONFIRMED


def test_past_deadline_closed():
    rec = {"deadline": "2020-01-01T00:00:00+00:00", "title": "Old"}
    assert classify_deadline_status(rec) == CLOSED_CONFIRMED
