"""Build 002K safeguard tests — ZERO live metered NextBid calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from m3.research import nextbid_controller as ctrl


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTBID_ENABLED", "false")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "true")
    monkeypatch.setenv("NEXTBID_API_KEY", "test_key_not_real")
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3.config import get_settings

    get_settings.cache_clear()
    from m3.research.nextbid_killswitch import engage_emergency_stop

    engage_emergency_stop(reason="test_002k")
    yield
    get_settings.cache_clear()


def _unlock(monkeypatch):
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(
        authorized=True,
        scope="build_002k_bounded_test",
        test_call_cap=100,
        max_additional_calls=100,
        note="test",
    )
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "test_used": 0,
            "month_used_local": 0,
            "jobs": {},
            "account_remaining": 5000,
            "account_used": 1000,
            "account_allowance": 6000,
        }
    )
    monkeypatch.setattr(ctrl, "acquisition_locked", lambda: False)


def test_locked_by_default():
    assert ctrl.acquisition_locked() is True


def test_101st_daily_call_blocked(monkeypatch):
    _unlock(monkeypatch)
    ctrl.save_budget_state(
        {
            **ctrl.load_budget_state(),
            "day_used": 100,
            "test_used": 0,
            "account_remaining": 5000,
            "account_used": 1000,
            "account_allowance": 6000,
        }
    )
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-a", 1)


def test_51st_job_call_blocked(monkeypatch):
    _unlock(monkeypatch)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-quota-remaining": "4999", "x-usage-used": "1001", "x-units": "1"}
    mock_resp.json.return_value = {"ok": True, "results": []}
    with patch.object(ctrl, "_http") as hf:
        client = MagicMock()
        client.request.return_value = mock_resp
        hf.return_value = client
        job = "job-51"
        for _ in range(50):
            ctrl.metered_request("GET", "/opportunities", job_id=job, endpoint="search", use_cache=False)
        with pytest.raises(ctrl.NextBidBudgetExhausted):
            ctrl.metered_request("GET", "/opportunities", job_id=job, endpoint="search", use_cache=False)


def test_101st_test_call_blocked(monkeypatch):
    _unlock(monkeypatch)
    ctrl.save_budget_state(
        {
            **ctrl.load_budget_state(),
            "day_used": 0,
            "test_used": 100,
            "account_remaining": 5000,
            "account_used": 1000,
            "account_allowance": 6000,
        }
    )
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-testcap", 1)


def test_monthly_reserve(monkeypatch):
    _unlock(monkeypatch)
    ctrl.save_budget_state(
        {
            **ctrl.load_budget_state(),
            "account_remaining": 1000,
            "account_used": 5000,
            "account_allowance": 6000,
        }
    )
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-reserve", 1)


def test_unknown_usage_stops(monkeypatch):
    _unlock(monkeypatch)
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "test_used": 0,
            "jobs": {},
            "account_remaining": None,
            "account_used": None,
            "account_allowance": None,
        }
    )
    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl._reserve("job-unk", 1)


def test_concurrent_budget(monkeypatch):
    _unlock(monkeypatch)
    # Only 5 remaining for the day
    ctrl.save_budget_state(
        {
            **ctrl.load_budget_state(),
            "day_used": 95,
            "test_used": 0,
            "account_remaining": 5000,
            "account_used": 1000,
            "account_allowance": 6000,
            "jobs": {},
        }
    )
    ok = 0
    blocked = 0

    def one(i):
        nonlocal ok, blocked
        try:
            ctrl._reserve(f"job-c-{i}", 1)
            ok += 1
        except ctrl.NextBidBudgetExhausted:
            blocked += 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, i) for i in range(12)]
        for f in as_completed(futs):
            f.result()
    assert ok == 5
    assert blocked == 7


def test_cache_avoids_metered(monkeypatch):
    _unlock(monkeypatch)
    from m3.research.nextbid_cache import cache_put

    cache_put("http", {"ok": True, "cached": True}, "/opportunities/NB1", "{}")
    data = ctrl.metered_request(
        "GET",
        "/opportunities/NB1",
        job_id="job-cache",
        endpoint="get_opportunity",
        nb_id="NB1",
        use_cache=True,
    )
    assert data.get("_cache_hit") is True
    assert ctrl.load_budget_state().get("day_used", 0) == 0


def test_scheduler_blocked():
    from m3.research.nextbid_schedule import run_scheduled_acquisition

    res = run_scheduled_acquisition(force=True)
    assert res.get("ok") is False


def test_run_acquisition_blocked():
    from m3.research.nextbid_acquire import run_nextbid_acquisition

    res = run_nextbid_acquisition(max_discovery_calls=1, max_detail=1)
    assert res.get("ok") is False
