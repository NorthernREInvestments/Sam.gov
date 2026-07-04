"""Tests for SAM.gov CSV import filters and mapping."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from csv_import_service import (
    _is_protected_status,
    _map_csv_row,
    _passes_import_filters,
    _passes_set_aside_filter,
    csv_upload_max_bytes,
    verify_csv_upload_password,
)


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
