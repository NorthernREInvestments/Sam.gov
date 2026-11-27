"""Free 8am/3pm and paid 8am schedule due logic."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from m3.research.schedule_policy import (
    due_free_slots,
    paid_due,
    parse_time_list,
    schedule_summary,
    slot_key,
)


def test_default_free_slots_8_and_15(monkeypatch):
    monkeypatch.delenv("FREE_REFRESH_TIMES", raising=False)
    monkeypatch.setenv("DAILY_REFRESH_HOUR", "8")
    monkeypatch.setenv("DAILY_REFRESH_MINUTE", "0")
    from m3.research import schedule_policy as sp

    slots = sp.free_refresh_slots()
    assert (8, 0) in slots
    assert (15, 0) in slots


def test_parse_free_times_env(monkeypatch):
    monkeypatch.setenv("FREE_REFRESH_TIMES", "08:00,15:00")
    from m3.research import schedule_policy as sp

    assert sp.free_refresh_slots() == [(8, 0), (15, 0)]


def test_due_both_slots_after_3pm():
    now = datetime(2026, 11, 26, 15, 5, tzinfo=ZoneInfo("America/Denver"))
    due = due_free_slots(now_local=now, already_ran=set())
    assert [d["slot"] for d in due] == [
        slot_key("2026-11-26", 8, 0),
        slot_key("2026-11-26", 15, 0),
    ]


def test_due_skips_already_ran_morning():
    now = datetime(2026, 11, 26, 15, 5, tzinfo=ZoneInfo("America/Denver"))
    ran = {slot_key("2026-11-26", 8, 0)}
    due = due_free_slots(now_local=now, already_ran=ran)
    assert len(due) == 1
    assert due[0]["hour"] == 15


def test_before_8am_nothing_due():
    now = datetime(2026, 11, 26, 7, 59, tzinfo=ZoneInfo("America/Denver"))
    assert due_free_slots(now_local=now, already_ran=set()) == []


def test_paid_due_once_at_8():
    morning = datetime(2026, 11, 26, 8, 1, tzinfo=ZoneInfo("America/Denver"))
    assert paid_due(now_local=morning, last_run_day=None)["due"] is True
    assert paid_due(now_local=morning, last_run_day="2026-11-26")["due"] is False
    early = datetime(2026, 11, 26, 7, 0, tzinfo=ZoneInfo("America/Denver"))
    assert paid_due(now_local=early, last_run_day=None)["due"] is False


def test_parse_time_list_and_summary(monkeypatch):
    assert parse_time_list("9:30, 15:00", default=("08:00",)) == [(9, 30), (15, 0)]
    monkeypatch.setenv("FREE_REFRESH_TIMES", "08:00,15:00")
    monkeypatch.setenv("PAID_REFRESH_TIME", "08:00")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    s = schedule_summary()
    assert s["free_refresh_times"] == ["08:00", "15:00"]
    assert s["paid_refresh_time"] == "08:00"
    assert s["scheduler_enabled_env"] is False


def test_scheduler_blocked_still(monkeypatch):
    # Keep existing fail-closed paid behavior under emergency stop
    from m3.research.nextbid_schedule import run_scheduled_acquisition

    res = run_scheduled_acquisition(force=True)
    assert res.get("ok") is False
