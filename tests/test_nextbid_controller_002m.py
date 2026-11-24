"""Build 002M controller safeguards — mocked, zero real provider spend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3.research import nextbid_controller as ctrl


@pytest.fixture()
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("NEXTBID_API_KEY", "test-key-not-real")
    monkeypatch.setenv("NEXTBID_ENABLED", "false")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "true")
    from m3.config import get_settings

    get_settings.cache_clear()
    (tmp_path / "nextbid_ledger").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    ctrl.revoke_owner_authorization()
    get_settings.cache_clear()


def test_002m_unlock_and_caps(isolated_ledger):
    ctrl.write_owner_authorization(approved_allocation_calls=1000, 
        authorized=True,
        scope=ctrl.BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=sorted(ctrl.BUILD_002M_ALLOWED_ENDPOINTS),
        reset_test_used=True,
        note="test",
    )
    # Scope never bypasses the emergency stop; daily override is clamped to the 100 ceiling
    assert ctrl.acquisition_locked() is True
    limits = ctrl._limits()
    assert limits["daily"] == 100
    assert limits["test"] == 300
    assert limits["job"] == 50
    assert limits["monthly_reserve"] == 1000


def test_002m_forbids_detail_endpoint(isolated_ledger, monkeypatch):
    ctrl.write_owner_authorization(approved_allocation_calls=1000, 
        authorized=True,
        scope=ctrl.BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=["search"],
        reset_test_used=True,
    )
    # Seed account usage so reserve path is reachable
    state = ctrl.load_budget_state()
    state["account_remaining"] = 2930
    state["account_used"] = 3070
    state["account_allowance"] = 6000
    ctrl.save_budget_state(state)

    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl.metered_request(
            "GET",
            "/opportunities/NB0001",
            job_id="t",
            endpoint="get_opportunity",
            nb_id="NB0001",
        )


def test_002m_forbids_non_search_path(isolated_ledger):
    ctrl.write_owner_authorization(approved_allocation_calls=1000, 
        authorized=True,
        scope=ctrl.BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=["search"],
        reset_test_used=True,
    )
    state = ctrl.load_budget_state()
    state["account_remaining"] = 2930
    state["account_used"] = 3070
    state["account_allowance"] = 6000
    ctrl.save_budget_state(state)
    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl.metered_request(
            "GET",
            "/opportunities/NB0001/documents",
            job_id="t",
            endpoint="search",  # even if mislabeled, path guard blocks
        )


def test_002m_override_clamped_blocks_101st(isolated_ledger, monkeypatch):
    monkeypatch.setattr(ctrl, "acquisition_locked", lambda: False)
    ctrl.write_owner_authorization(approved_allocation_calls=1000, 
        authorized=True,
        scope=ctrl.BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=["search"],
        reset_test_used=True,
    )
    state = ctrl.load_budget_state()
    state["account_remaining"] = 2930
    state["account_used"] = 3070
    state["account_allowance"] = 6000
    state["test_used"] = 100
    state["day_used"] = 100
    ctrl.save_budget_state(state)
    with pytest.raises(ctrl.NextBidBudgetExhausted):
        ctrl._reserve("job-x", 1)
