"""Central NextBid / SAM budget guards — mocked state only, no network."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from m3.research import nextbid_controller as nc
from m3.research import sam_budget as sb


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(nc, "_ledger_dir", lambda: tmp_path)
    monkeypatch.setattr(nc, "emergency_stop_status", lambda: {"engaged": False, "metered_calls_allowed": True})
    settings = SimpleNamespace(nextbid_enabled=True, nextbid_emergency_stop=False, nextbid_budget_reserve=1000,
                               nextbid_job_call_limit=50, nextbid_daily_call_limit=100)
    monkeypatch.setattr(nc, "get_settings", lambda: settings)
    auth = {"authorized": True, "approved_allocation_calls": 1000, "written_at": "t1"}
    monkeypatch.setattr(nc, "owner_authorization", lambda: auth)
    nc.save_budget_state({"account_remaining": 5000, "account_used": 1000, "account_allowance": 6000, "jobs": {}})
    return SimpleNamespace(settings=settings, auth=auth, path=tmp_path)


def _spend(job, n):
    ok = 0
    for _ in range(n):
        r = nc._reserve(job, 1, endpoint="get_opportunity")
        nc._commit(r, endpoint="get_opportunity", ok=True, meta={})
        ok += 1
    return ok


def test_51st_job_request_blocked(ledger):
    assert _spend("job-a", 50) == 50
    with pytest.raises(nc.NextBidBudgetExhausted, match="job_limit"):
        nc._reserve("job-a", 1, endpoint="get_opportunity")


def test_101st_daily_request_blocked_across_new_jobs(ledger):
    _spend("job-a", 50)
    _spend("job-b", 50)
    with pytest.raises(nc.NextBidBudgetExhausted, match="daily"):
        nc._reserve("job-c", 1, endpoint="get_opportunity")


def test_monthly_reserve_takes_precedence(ledger):
    st = nc.load_budget_state()
    st["account_remaining"] = 1000
    nc.save_budget_state(st)
    with pytest.raises(nc.NextBidBudgetExhausted, match="monthly_reserve"):
        nc._reserve("job-a", 1, endpoint="search")


def test_zero_approved_allocation_blocks_first_request(ledger):
    ledger.auth["approved_allocation_calls"] = 0
    assert nc.acquisition_locked()
    with pytest.raises(nc.NextBidUnauthorized):
        nc._reserve("job-a", 1, endpoint="search")


def test_allocation_ceiling(ledger):
    ledger.auth["approved_allocation_calls"] = 3
    _spend("job-a", 3)
    with pytest.raises(nc.NextBidBudgetExhausted, match="approved_allocation"):
        nc._reserve("job-a", 1, endpoint="search")


def test_emergency_stop_beats_scoped_authorization(ledger, monkeypatch):
    ledger.auth["scope"] = nc.BUILD_002Q_SCOPE
    monkeypatch.setattr(nc, "emergency_stop_status", lambda: {"engaged": True})
    assert nc.acquisition_locked()


def test_provider_credits_count_against_daily_ceiling(ledger):
    r = nc._reserve("job-a", 1, endpoint="read_file")
    nc._commit(r, endpoint="read_file", ok=True, meta={"units_hdr": 99})
    with pytest.raises(nc.NextBidBudgetExhausted, match="daily"):
        _spend("job-b", 2)


def test_counters_survive_restart(ledger):
    _spend("job-a", 10)
    state = nc.load_budget_state()  # fresh read from disk, as a new process would
    assert state["day_used"] == 10 and state["day_credits"] == 10 and state["allocation_used"] == 10


def test_concurrent_workers_cannot_exceed_daily(ledger):
    results = []

    def worker(i):
        try:
            r = nc._reserve(f"job-{i % 5}", 1, endpoint="search")
            nc._commit(r, endpoint="search", ok=True, meta={})
            results.append(1)
        except nc.NextBidBudgetExhausted:
            results.append(0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(160)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sum(results) == 100
    assert nc.load_budget_state()["day_used"] == 100


def test_metered_request_refuses_while_locked(monkeypatch):
    monkeypatch.setattr(nc, "acquisition_locked", lambda: True)
    called = []
    monkeypatch.setattr(nc, "_http", lambda: called.append(1))
    with pytest.raises(nc.NextBidUnauthorized):
        nc.metered_request("GET", "/opportunities", job_id="j", endpoint="search")
    assert not called


def test_live_configuration_is_locked():
    assert nc.acquisition_locked()


@pytest.fixture
def sam(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_dir", lambda: tmp_path)
    settings = SimpleNamespace(sam_live_enabled=True)
    monkeypatch.setattr(sb, "get_settings", lambda: settings)
    return tmp_path


def test_sam_zero_allocation_blocks_first_call(sam):
    with pytest.raises(sb.SamBudgetError, match="locked"):
        sb.reserve_sam_call("j")


def test_sam_11th_daily_call_blocked(sam):
    (sam / "owner_authorization.json").write_text('{"authorized": true, "approved_allocation_calls": 50, "written_at": "t"}')
    for _ in range(10):
        sb.commit_sam_call(sb.reserve_sam_call("j"), sent=True)
    with pytest.raises(sb.SamBudgetError, match="daily"):
        sb.reserve_sam_call("j")


def test_sam_disabled_in_live_config():
    assert sb.sam_status()["locked"] is True
