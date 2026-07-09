"""Tests for SAM.gov CSV import filters and mapping."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from csv_import_service import (
    _csv_pricing_fields_changed,
    _csv_row_fields_changed,
    _csv_row_keep_score,
    _is_protected_status,
    _map_csv_row,
    _normalize_solicitation_number,
    _passes_import_filters,
    _passes_set_aside_filter,
    csv_upload_max_bytes,
    verify_csv_upload_password,
)
from models import CsvOpportunity


def test_set_aside_filter():
    assert _passes_set_aside_filter({"SetASideCode": "Total Small Business Set-Aside (FAR 19.5)"})
    assert _passes_set_aside_filter({"SetASide": "SBA"})
    assert not _passes_set_aside_filter({"SetASideCode": "8A Competed"})


def test_import_filters_future_deadline(monkeypatch):
    monkeypatch.setattr("csv_import_service.get_naics_codes", lambda: ["561720"])
    future = (date.today() + timedelta(days=30)).strftime("%m/%d/%Y")
    row = {
        "Active": "Yes",
        "NaicsCode": "561720",
        "SetASideCode": "Total Small Business",
        "ResponseDeadLine": future,
        "NoticeId": "abc123",
    }
    assert _passes_import_filters(row)
    row["Active"] = "No"
    assert not _passes_import_filters(row)
    row["Active"] = "Yes"
    row["NaicsCode"] = "999999"
    assert not _passes_import_filters(row)


def test_normalize_solicitation_number():
    assert _normalize_solicitation_number("692M15-26-R-00008") == "692M15-26-R-00008"
    assert _normalize_solicitation_number("  rfq_15ddhq26q00000143 ") == "RFQ_15DDHQ26Q00000143"
    assert _normalize_solicitation_number("") is None


def test_csv_row_keep_score_prefers_protected_and_watchlist():
    base = CsvOpportunity(
        notice_id="a",
        title="Test",
        status="New",
        due_date=date.today() + timedelta(days=10),
    )
    pursuing = CsvOpportunity(
        notice_id="b",
        title="Test",
        status="Pursuing",
        due_date=date.today() + timedelta(days=3),
    )
    assert _csv_row_keep_score(pursuing) > _csv_row_keep_score(base)
    watchlist = CsvOpportunity(
        notice_id="c",
        title="Test",
        status="New",
        due_date=date.today() + timedelta(days=3),
        watchlist_match_confidence="High",
    )
    assert _csv_row_keep_score(watchlist) > _csv_row_keep_score(base)


def test_parse_amendment_number_from_title():
    from csv_import_service import _parse_amendment_number

    row = {"Title": "Ground Maintenance Services Amendment 0003"}
    assert _parse_amendment_number(row) == 3
    row = {"AmendmentNumber": "5"}
    assert _parse_amendment_number(row) == 5


def test_merge_csv_sam_meta_tracks_notice_ids():
    from csv_import_service import _merge_csv_sam_meta

    merged = _merge_csv_sam_meta(
        {"merged_notice_ids": ["a"], "merged_notice_count": 1, "posted_date": "2026-07-01T00:00:00"},
        {"merged_notice_ids": ["b"], "merged_notice_count": 1, "posted_date": "2026-07-04T00:00:00", "amendment_number": 2},
    )
    assert merged["merged_notice_count"] == 2
    assert set(merged["merged_notice_ids"]) == {"a", "b"}
    assert merged["amendment_number"] == 2


def test_import_filters_past_deadline(monkeypatch):
    monkeypatch.setattr("csv_import_service.get_naics_codes", lambda: ["561720"])
    past = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
    row = {
        "Active": "Yes",
        "NaicsCode": "561720",
        "SetASideCode": "Total Small Business",
        "ResponseDeadLine": past,
        "NoticeId": "abc123",
    }
    assert not _passes_import_filters(row)
    today = date.today().strftime("%m/%d/%Y")
    row["ResponseDeadLine"] = today
    assert _passes_import_filters(row)


def test_map_csv_row():
    mapped = _map_csv_row(
        {
            "NoticeId": "N-1",
            "Title": "Janitorial",
            "Sol#": "SOL-1",
            "Department/Ind.Agency": "VA",
            "Office": "Network 22",
            "ResponseDeadLine": "12/31/2026",
            "NaicsCode": "561720",
            "SetASideCode": "SBA",
            "PopCity": "Denver",
            "PopState": "CO",
            "PrimaryContactFullname": "Jane Doe",
            "PrimaryContactEmail": "jane@va.gov",
            "Link": "https://sam.gov/opp/N-1/view",
            "Description": "Clean buildings",
        }
    )
    assert mapped["notice_id"] == "N-1"
    assert mapped["agency"] == "VA"
    assert mapped["location_city"] == "Denver"
    assert mapped["co_email"] == "jane@va.gov"
    assert mapped["status"] == "New"


def test_protected_status():
    assert _is_protected_status("Pursuing")
    assert _is_protected_status("won")
    assert not _is_protected_status("New")


def test_upload_password_requires_env(monkeypatch):
    monkeypatch.delenv("CSV_UPLOAD_PASSWORD", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert not verify_csv_upload_password("secret")
    monkeypatch.setenv("CSV_UPLOAD_PASSWORD", "secret")
    assert verify_csv_upload_password("secret")


def test_csv_upload_max_bytes_default(monkeypatch):
    monkeypatch.delenv("CSV_UPLOAD_MAX_MB", raising=False)
    assert csv_upload_max_bytes() == 250 * 1024 * 1024


def test_csv_upload_max_bytes_env(monkeypatch):
    monkeypatch.setenv("CSV_UPLOAD_MAX_MB", "300")
    assert csv_upload_max_bytes() == 300 * 1024 * 1024


def test_csv_row_fields_changed_detects_title_update():
    existing = type(
        "Row",
        (),
        {
            "notice_id": "N-1",
            "title": "Old title",
            "agency": "VA",
            "naics_code": "561720",
            "location_city": "Denver",
            "location_state": "CO",
            "description": "Clean",
            "due_date": date(2026, 12, 31),
            "set_aside": "SBA",
            "solicitation_number": "SOL-1",
        },
    )()
    mapped = _map_csv_row(
        {
            "NoticeId": "N-1",
            "Title": "New title",
            "Department/Ind.Agency": "VA",
            "ResponseDeadLine": "12/31/2026",
            "NaicsCode": "561720",
            "SetASideCode": "SBA",
            "PopCity": "Denver",
            "PopState": "CO",
            "Sol#": "SOL-1",
            "Description": "Clean",
        }
    )
    assert _csv_row_fields_changed(existing, mapped)
    assert _csv_pricing_fields_changed(existing, mapped)


def test_csv_row_fields_unchanged_when_identical():
    mapped = _map_csv_row(
        {
            "NoticeId": "N-1",
            "Title": "Janitorial",
            "Department/Ind.Agency": "VA",
            "ResponseDeadLine": "12/31/2026",
            "NaicsCode": "561720",
            "SetASideCode": "SBA",
            "PopCity": "Denver",
            "PopState": "CO",
            "Sol#": "SOL-1",
            "Description": "Clean buildings",
        }
    )
    existing = type("Row", (), mapped)()
    assert not _csv_row_fields_changed(existing, mapped)
    assert not _csv_pricing_fields_changed(existing, mapped)
