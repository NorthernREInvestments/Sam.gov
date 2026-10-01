"""Unified SAM budget: one 10-credit pool across file ledger + DB."""

from __future__ import annotations

import discovery.sam_budgeted_client as sbc
import api_budget


def test_unified_pool_blocks_second_ten(tmp_path, monkeypatch):
    monkeypatch.setattr(sbc, "ART", tmp_path)
    monkeypatch.setattr(sbc, "LEDGER_PATH", tmp_path / "sam_call_ledger.json")
    monkeypatch.setattr(sbc, "LOCK_PATH", tmp_path / ".sam_budget.lock")
    monkeypatch.setattr(sbc, "CACHE_DIR", tmp_path / "cache")
    sbc.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sbc, "sam_daily_call_budget", lambda: 10)
    monkeypatch.setattr(sbc, "_db_sam_used_today", lambda: 8)
    monkeypatch.setattr(api_budget, "set_sam_used_today", lambda used: None)

    led = sbc.load_ledger()
    day = sbc.budget_day_key()
    led["days"] = {day: {"live_calls": 5, "cache_hits": 0, "live_rows": 0, "unique_rows": 0, "failures": 0}}
    sbc.save_ledger(led)

    # max(5, 8) = 8 → 2 remaining
    assert sbc.unified_calls_used_today() == 8
    assert sbc.can_afford_live_calls(2) is True
    assert sbc.can_afford_live_calls(3) is False

    assert sbc.consume_live_credits(2, reason="test") is True
    assert sbc.unified_calls_used_today() == 10
    assert sbc.can_afford_live_calls(1) is False
    assert sbc.consume_live_credits(1, reason="test_over") is False


def test_api_budget_delegates_to_unified(monkeypatch):
    monkeypatch.setattr(sbc, "can_afford_live_calls", lambda credits=1: credits <= 1)
    monkeypatch.setattr(sbc, "consume_live_credits", lambda credits=1, reason="x": credits == 1)
    assert api_budget.can_spend_sam(1) is True
    assert api_budget.can_spend_sam(2) is False
    assert api_budget.record_sam_usage(1) is True
    assert api_budget.record_sam_usage(2) is False
