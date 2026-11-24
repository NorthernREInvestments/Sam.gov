"""06:00 / 14:00 daily discovery slots: prepared but disabled behind three independent gates."""
from __future__ import annotations

from datetime import datetime, timezone

from m3.research import nextbid_schedule as ns


def test_next_slot_morning_and_afternoon():
    # 2026-11-23 11:00 UTC = 05:00 America/Chicago -> 06:00 CST = 12:00 UTC
    assert ns.next_discovery_slot(datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-23T12:00:00+00:00"
    # 10:00 CST -> 14:00 CST = 20:00 UTC
    assert ns.next_discovery_slot(datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-23T20:00:00+00:00"
    # after 14:00 CST -> next day 06:00
    assert ns.next_discovery_slot(datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-24T12:00:00+00:00"


def test_plan_reports_all_gates_closed_by_default():
    plan = ns.schedule_plan(datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))
    assert plan["slots_local"] == ["06:00", "14:00"]
    assert plan["gates"]["discovery_schedule_enabled"] is False
    assert plan["would_run"] is False


def test_scheduled_run_refuses_while_locked():
    r = ns.run_scheduled_acquisition(force=True)
    assert r["ok"] is False and r["skipped"] in {"emergency_stop", "nextbid_disabled", "daily_schedule_disabled"}


def test_scheduler_will_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_status_path", lambda: tmp_path / "scheduler_status.json")
    assert ns.start_nextbid_scheduler() is False
    assert (tmp_path / "scheduler_status.json").exists()
