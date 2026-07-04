"""Tests for CSV attachment queue priority and budget cap."""

from __future__ import annotations

from datetime import date, timedelta

from csv_attachment_queue_service import (
    TIER_HIGH,
    TIER_OTHER,
    TIER_POSSIBLE,
    compute_queue_priority,
    csv_attachment_budget_allowed,
)
from models import CsvOpportunity


def _row(**kwargs) -> CsvOpportunity:
    base = dict(
        id=1,
        notice_id="N-1",
        title="Test",
        due_date=date.today() + timedelta(days=10),
        watchlist_match_confidence=None,
    )
    base.update(kwargs)
    return CsvOpportunity(**base)


def test_priority_order_high_before_possible_before_other():
    due_soon = date.today() + timedelta(days=5)
    due_later = date.today() + timedelta(days=30)
    high, _ = compute_queue_priority(_row(watchlist_match_confidence="High", due_date=due_later))
    possible, _ = compute_queue_priority(_row(watchlist_match_confidence="Possible", due_date=due_soon))
    other, _ = compute_queue_priority(_row(due_date=due_soon))
    assert high < possible < other
    assert high >= TIER_HIGH
    assert possible >= TIER_POSSIBLE
    assert other >= TIER_OTHER


def test_priority_within_tier_soonest_due_first():
    soon = compute_queue_priority(_row(due_date=date.today() + timedelta(days=3)))[0]
    later = compute_queue_priority(_row(due_date=date.today() + timedelta(days=20)))[0]
    assert soon < later


def test_budget_blocked_at_80_percent(monkeypatch):
    monkeypatch.setenv("CSV_ATTACHMENT_BUDGET_THRESHOLD", "0.8")
    monkeypatch.setattr(
        "csv_attachment_queue_service.get_usage_snapshot",
        lambda: {"sam_daily_limit": 10, "sam_used_today": 8, "sam_remaining": 2},
    )
    assert not csv_attachment_budget_allowed()
    monkeypatch.setattr(
        "csv_attachment_queue_service.get_usage_snapshot",
        lambda: {"sam_daily_limit": 10, "sam_used_today": 7, "sam_remaining": 3},
    )
    assert csv_attachment_budget_allowed()
