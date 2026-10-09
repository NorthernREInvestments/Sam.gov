"""M3 SAM API credit ledger reconciliation — REAL vs cache/public/blocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import discovery.sam_budgeted_client as sbc


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(sbc, "ART", tmp_path)
    monkeypatch.setattr(sbc, "LEDGER_PATH", tmp_path / "sam_call_ledger.json")
    monkeypatch.setattr(sbc, "LOCK_PATH", tmp_path / ".sam_budget.lock")
    monkeypatch.setattr(sbc, "CACHE_DIR", tmp_path / "cache")
    sbc.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sbc, "_db_sam_used_today", lambda: 0)
    monkeypatch.setenv("SAM_DAILY_CALL_BUDGET", "10")
    monkeypatch.setenv("SAM_DAILY_RESERVE_CALLS", "1")
    import api_budget

    monkeypatch.setattr(api_budget, "set_sam_used_today", lambda used: None)


def test_dual_increment_inflation_reconciled(tmp_path, monkeypatch):
    """Simulate prior bug: live_calls=13 while only 10 real budget credits."""
    _iso(tmp_path, monkeypatch)
    day = sbc.budget_day_key()
    led = sbc.load_ledger()
    led["days"][day] = {
        "live_calls": 13,
        "cache_hits": 4,
        "live_rows": 100,
        "unique_rows": 80,
        "failures": 0,
        "internal_attempts": 20,
    }
    # 10 real budget consumes + cache/audit noise that used to += live_calls
    for i in range(10):
        led.setdefault("entries", []).append(
            {
                "date": day,
                "endpoint": "budget",
                "credits_consumed": 1,
                "counts_as_real_api": True,
                "unified_used_after": i + 1,
                "cache_hit": False,
                "reason": f"live_{i}",
                "success": True,
            }
        )
    for _ in range(4):
        led["entries"].append(
            {
                "date": day,
                "endpoint": sbc.SAM_SEARCH_URL,
                "credits_consumed": 0,
                "counts_as_real_api": False,
                "cache_hit": True,
                "reason": "search:cache_hit",
                "success": True,
            }
        )
    # Legacy audit rows that wrongly carried credits_consumed=1 (must NOT count)
    for _ in range(3):
        led["entries"].append(
            {
                "date": day,
                "endpoint": sbc.SAM_SEARCH_URL,
                "credits_consumed": 1,
                "counts_as_real_api": False,
                "cache_hit": False,
                "reason": "legacy_audit_dual",
                "success": True,
            }
        )
    sbc.save_ledger(led)

    recon = sbc.reconstruct_real_api_calls_today()
    assert recon["REAL_SAM_API_CALLS_TODAY"] == 10
    assert recon["CACHE_HITS"] >= 4
    assert recon["STALE_LIVE_CALLS_FIELD"] == 13
    assert recon["REMAINING_REAL_CREDITS"] == 0
    assert any("dual-increment" in x or "stale" in x.lower() for x in recon["WHY_PRIOR_SHOWED_INFLATED"])

    out = sbc.reconcile_sam_credit_ledger(persist=True)
    assert out["BEFORE_LIVE_CALLS_FIELD"] == 13
    assert out["AFTER_LIVE_CALLS_FIELD"] == 10
    assert sbc.load_ledger()["days"][day]["live_calls"] == 10

    owner = sbc.owner_credit_dashboard()
    assert owner["REAL_SAM_API_CALLS_TODAY"] == 10
    assert owner["GATES"]["REAL_CALL_COUNTER_SEPARATE"] is True
    assert owner["GATES"]["CACHE_NOT_COUNTED_AS_API"] is True
    assert owner["GATES"]["PUBLIC_FETCHES_NOT_COUNTED_AS_API"] is True
    assert owner["GATES"]["OWNER_COUNTER_TRUSTWORTHY"] is True
    assert owner["GATES"]["11TH_CALL_IMPOSSIBLE"] is True


def test_eleventh_call_blocked_before_http(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    day = sbc.budget_day_key()
    led = sbc.load_ledger()
    led["days"][day] = {"live_calls": 10, "cache_hits": 0, "live_rows": 0, "unique_rows": 0}
    for i in range(10):
        led.setdefault("entries", []).append(
            {
                "date": day,
                "endpoint": "budget",
                "credits_consumed": 1,
                "counts_as_real_api": True,
                "unified_used_after": i + 1,
                "cache_hit": False,
                "reason": f"c{i}",
                "success": True,
            }
        )
    sbc.save_ledger(led)

    transport = MagicMock()
    resp = sbc.search_opportunities(
        {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 10, "offset": 0},
        reason="eleventh",
        authorize_live=True,
        transport=transport,
    )
    assert resp["_meta"]["status"] == sbc.SAM_DAILY_BUDGET_EXHAUSTED
    assert resp["_meta"]["credits_consumed"] == 0
    assert resp["_meta"].get("blocked") is True
    assert transport.get.call_count == 0
    assert sbc.unified_calls_used_today() == 10
    recon = sbc.reconstruct_real_api_calls_today()
    assert recon["BLOCKED_OVER_CAP_ATTEMPTS"] >= 1


def test_cache_and_public_reasons_not_real(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    day = sbc.budget_day_key()
    led = sbc.load_ledger()
    led["days"][day] = {"live_calls": 1, "cache_hits": 0}
    led["entries"] = [
        {
            "date": day,
            "endpoint": "budget",
            "credits_consumed": 1,
            "counts_as_real_api": True,
            "unified_used_after": 1,
            "cache_hit": False,
            "reason": "live",
            "success": True,
        },
        {
            "date": day,
            "endpoint": "public",
            "credits_consumed": 0,
            "counts_as_real_api": False,
            "cache_hit": False,
            "reason": "noticedesc_public_fetch",
            "success": True,
        },
        {
            "date": day,
            "endpoint": "public",
            "credits_consumed": 0,
            "counts_as_real_api": False,
            "cache_hit": False,
            "reason": "agency_url_fetch",
            "success": True,
        },
        {
            "date": day,
            "endpoint": sbc.SAM_SEARCH_URL,
            "credits_consumed": 0,
            "counts_as_real_api": False,
            "cache_hit": True,
            "reason": "cache_hit",
            "success": True,
        },
    ]
    sbc.save_ledger(led)
    recon = sbc.reconstruct_real_api_calls_today()
    assert recon["REAL_SAM_API_CALLS_TODAY"] == 1
    assert recon["CACHE_HITS"] >= 1
    assert recon["PUBLIC_NOTICEDESC_FETCHES"] >= 1
    assert recon["AGENCY_URL_FETCHES"] >= 1
    assert recon["REMAINING_REAL_CREDITS"] == 9


def test_env_cannot_raise_hard_limit(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    monkeypatch.setenv("SAM_DAILY_CALL_BUDGET", "50")
    assert sbc.sam_daily_call_budget() == 10
