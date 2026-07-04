"""Tests for background CSV upload job stale detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from csv_upload_job import _processing_is_stale, _stale_error_message


def test_processing_stale_when_zero_rows_for_ten_minutes():
    started = datetime.now(timezone.utc) - timedelta(minutes=11)
    state = {
        "status": "processing",
        "started_at": started.isoformat(),
        "updated_at": started.isoformat(),
        "rows_scanned": 0,
    }
    assert _processing_is_stale(state)
    assert "stalled before scanning" in _stale_error_message(state)


def test_processing_not_stale_when_rows_are_moving():
    now = datetime.now(timezone.utc)
    state = {
        "status": "processing",
        "started_at": (now - timedelta(minutes=5)).isoformat(),
        "updated_at": now.isoformat(),
        "rows_scanned": 4000,
    }
    assert not _processing_is_stale(state)


def test_processing_stale_without_recent_heartbeat():
    now = datetime.now(timezone.utc)
    state = {
        "status": "processing",
        "started_at": (now - timedelta(minutes=20)).isoformat(),
        "updated_at": (now - timedelta(minutes=16)).isoformat(),
        "rows_scanned": 8000,
    }
    assert _processing_is_stale(state)
