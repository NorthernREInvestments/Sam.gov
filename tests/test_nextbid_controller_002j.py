"""Build 002J — controller protection tests. ZERO live metered NextBid calls."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from m3.research import nextbid_controller as ctrl
from m3.research.nextbid_killswitch import engage_emergency_stop, emergency_stop_status


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTBID_ENABLED", "false")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "true")
    monkeypatch.setenv("NEXTBID_API_KEY", "test_key_not_real")
    # Point data root to temp
    from m3.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    engage_emergency_stop(reason="test")
    yield
    get_settings.cache_clear()


def test_acquisition_locked_by_default():
    assert ctrl.acquisition_locked() is True
    st = ctrl.controller_status()
    assert st["locked"] is True


def test_metered_request_blocked_when_locked():
    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl.metered_request(
            "GET",
            "/opportunities",
            job_id="job-test",
            endpoint="search",
            params={"q": "x"},
        )


def test_scheduler_skip_under_lock():
    from m3.research.nextbid_schedule import run_scheduled_acquisition

    res = run_scheduled_acquisition(force=True)
    assert res.get("ok") is False
    assert res.get("skipped") in {"emergency_stop", "nextbid_disabled"}


def test_daily_and_job_limits_with_mocks(tmp_path, monkeypatch):
    """Simulate unlock + usage known, prove daily/job caps stop spend."""
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    # Owner auth file required
    ctrl.write_owner_authorization(authorized=True, max_additional_calls=100, note="test_only")
    # Seed budget state with known allowance
    state = {
        "day": str(__import__("datetime").date.today()),
        "day_used": 0,
        "month_used_local": 0,
        "jobs": {},
        "account_remaining": 5000,
        "account_used": 1000,
        "account_allowance": 6000,
        "reconciled_provider_used": 1000,
        "local_metered_since_reconcile": 0,
        "provider_discrepancy_stop": False,
    }
    ctrl.save_budget_state(state)
    assert ctrl.acquisition_locked() is False

    # Mock HTTP
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-quota-remaining": "4999", "x-usage-used": "1001", "x-units": "1"}
    mock_resp.text = '{"ok":true,"results":[]}'
    mock_resp.json.return_value = {"ok": True, "results": []}

    with patch.object(ctrl, "_http") as http_factory:
        client = MagicMock()
        client.request.return_value = mock_resp
        http_factory.return_value = client

        job = "job-limit-test"
        # Job limit default 50 — unique params + no cache so each call meters
        for i in range(50):
            ctrl.metered_request(
                "GET",
                "/opportunities",
                job_id=job,
                endpoint="search",
                params={"page": i, "q": f"limit-{i}"},
                use_cache=False,
            )
        with pytest.raises(ctrl.NextBidBudgetExhausted):
            ctrl.metered_request(
                "GET",
                "/opportunities",
                job_id=job,
                endpoint="search",
                params={"page": 50, "q": "limit-50"},
                use_cache=False,
            )

    # Daily ceiling: new job should still be limited by day_used
    st = ctrl.load_budget_state()
    assert st["day_used"] == 50


def test_daily_hard_ceiling_100(monkeypatch):
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(authorized=True, note="test")
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 100,
            "month_used_local": 100,
            "jobs": {},
            "account_remaining": 5000,
            "account_used": 1100,
            "account_allowance": 6000,
        }
    )
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-daily", 1)


def test_monthly_reserve_floor(monkeypatch):
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(authorized=True, note="test")
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "month_used_local": 0,
            "jobs": {},
            "account_remaining": 1000,  # at reserve floor
            "account_used": 5000,
            "account_allowance": 6000,
        }
    )
    monkeypatch.setattr(ctrl, "acquisition_locked", lambda: False)
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-reserve", 1)


def test_unknown_usage_stops(monkeypatch):
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(authorized=True, note="test")
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "jobs": {},
            "account_remaining": None,
            "account_used": None,
            "account_allowance": None,
        }
    )
    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl._reserve("job-unknown", 1)


def test_inventory_dedup_no_api():
    from m3.research.nextbid_inventory import content_hash, upsert_inventory

    items = [
        {"nb_id": "NB1", "title": "Two (2) Widgets", "official_url": "https://example.gov/1"},
        {"nb_id": "NB1", "title": "Two (2) Widgets", "official_url": "https://example.gov/1"},
    ]
    r1 = upsert_inventory(items)
    r2 = upsert_inventory(items)
    assert r1["total"] == 1
    assert r2["unchanged"] >= 1
    assert content_hash(items[0]) == content_hash(items[1])


def test_run_nextbid_acquisition_blocked():
    from m3.research.nextbid_acquire import run_nextbid_acquisition

    res = run_nextbid_acquisition(max_discovery_calls=1, max_detail=1)
    assert res.get("ok") is False
    assert res.get("error") == "emergency_stop"
