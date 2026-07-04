"""Tests for CSV-only SAM attachment policy."""

from __future__ import annotations

from datetime import date, timedelta

from csv_attachment_policy import (
    notice_id_csv_attachment_eligible,
    sam_attachments_csv_only,
    sam_attachments_csv_only_until,
)
from models import CsvOpportunity


class _FakeQuery:
    def __init__(self, found: bool):
        self._found = found

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return object() if self._found else None


class _FakeSession:
    def __init__(self, found: bool):
        self._found = found

    def query(self, model):
        return _FakeQuery(self._found)


def test_csv_only_defaults_enabled_until_july_10(monkeypatch):
    monkeypatch.delenv("SAM_ATTACHMENTS_CSV_ONLY", raising=False)
    monkeypatch.delenv("SAM_ATTACHMENTS_CSV_ONLY_UNTIL", raising=False)
    assert sam_attachments_csv_only_until() == date(2026, 7, 10)
    assert sam_attachments_csv_only()


def test_csv_only_disabled_after_until_date(monkeypatch):
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY", "true")
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY_UNTIL", (date.today() - timedelta(days=1)).isoformat())
    assert not sam_attachments_csv_only()


def test_notice_eligible_when_not_csv_only_mode(monkeypatch):
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY", "false")
    session = _FakeSession(found=False)
    assert notice_id_csv_attachment_eligible(session, "ANY-NOTICE")


def test_notice_ineligible_without_csv_row(monkeypatch):
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY", "true")
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY_UNTIL", (date.today() + timedelta(days=7)).isoformat())
    session = _FakeSession(found=False)
    assert not notice_id_csv_attachment_eligible(session, "NOT-IN-CSV")


def test_notice_eligible_with_csv_row(monkeypatch):
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY", "true")
    monkeypatch.setenv("SAM_ATTACHMENTS_CSV_ONLY_UNTIL", (date.today() + timedelta(days=7)).isoformat())
    session = _FakeSession(found=True)
    assert notice_id_csv_attachment_eligible(session, "CSV-123")
