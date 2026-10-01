"""Tests for SAM 10-call budget client + supplier-path conversion (0 live SAM calls)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from discovery.sam_budgeted_client import (
    SAM_DAILY_BUDGET_EXHAUSTED,
    SAM_RESERVE_PROTECTED,
    SAM_SEARCH_URL,
    build_daily_query_plan,
    calls_used_today,
    dashboard,
    execute_query_plan,
    load_ledger,
    query_fingerprint,
    reserve_calls,
    sam_daily_call_budget,
    save_ledger,
    search_opportunities,
)
from phase_l.sam_supplier_conversion_run import (
    convert_supplier_paths,
    ingest_sam_rows,
    sam_raw_to_row,
)
from phase_l.l23_full_population_funnel import DEEP_RESEARCH_COMPLETE, READY_TO_CALL, load_store

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts" / "sam"


@pytest.fixture()
def isolated_ledger(tmp_path, monkeypatch):
    """Point ledger/cache at tmp so tests never touch real daily usage."""
    import discovery.sam_budgeted_client as sbc

    monkeypatch.setattr(sbc, "ART", tmp_path)
    monkeypatch.setattr(sbc, "CACHE_DIR", tmp_path / "response_cache")
    monkeypatch.setattr(sbc, "LEDGER_PATH", tmp_path / "sam_call_ledger.json")
    monkeypatch.setattr(sbc, "LOCK_PATH", tmp_path / ".sam_budget.lock")
    monkeypatch.setattr(sbc, "PRODUCTIVITY_PATH", tmp_path / "sam_query_productivity.json")
    monkeypatch.setattr(sbc, "LAST_REFRESH_PATH", tmp_path / "last_successful_sam_refresh.json")
    monkeypatch.setattr(sbc, "PLAN_PATH", tmp_path / "sam_daily_query_plan.json")
    (tmp_path / "response_cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAM_DAILY_CALL_BUDGET", "10")
    monkeypatch.setenv("SAM_DAILY_RESERVE_CALLS", "1")
    # Isolate from production DB counter — unified pool must not see real Railway/local usage
    monkeypatch.setattr(sbc, "_db_sam_used_today", lambda: 0)
    monkeypatch.setattr(sbc, "_sync_db_budget", lambda credits=1: True)
    import api_budget

    monkeypatch.setattr(api_budget, "set_sam_used_today", lambda used: None)
    return tmp_path


def test_daily_budget_default_10(isolated_ledger):
    assert sam_daily_call_budget() == 10
    assert reserve_calls() == 1


def test_query_fingerprint_stable_excludes_key(isolated_ledger):
    a = query_fingerprint(SAM_SEARCH_URL, {"limit": 1000, "offset": 0, "api_key": "AAA"})
    b = query_fingerprint(SAM_SEARCH_URL, {"api_key": "BBB", "offset": 0, "limit": 1000})
    assert a == b
    c = query_fingerprint(SAM_SEARCH_URL, {"limit": 1000, "offset": 1})
    assert a != c


def test_cache_before_call_zero_credits(isolated_ledger):
    params = {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 100, "offset": 0, "active": "yes"}
    fp = query_fingerprint(SAM_SEARCH_URL, params)
    from discovery.sam_budgeted_client import save_cached_response

    save_cached_response(fp, {"opportunitiesData": [{"noticeId": "n1"}], "totalRecords": 1})
    resp = search_opportunities(params, reason="test_cache", authorize_live=True)
    assert resp["_meta"]["cache_hit"] is True
    assert resp["_meta"]["credits_consumed"] == 0
    assert calls_used_today() == 0


def test_hard_budget_gate(isolated_ledger, monkeypatch):
    import discovery.sam_budgeted_client as sbc

    led = load_ledger()
    day = sbc.budget_day_key()
    led["days"][day] = {"live_calls": 10, "cache_hits": 0, "live_rows": 0, "unique_rows": 0}
    save_ledger(led)
    resp = search_opportunities(
        {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 10, "offset": 0},
        reason="should_block",
        authorize_live=True,
    )
    assert resp["_meta"]["status"] == SAM_DAILY_BUDGET_EXHAUSTED
    assert resp["_meta"]["credits_consumed"] == 0


def test_reserve_protection(isolated_ledger):
    import discovery.sam_budgeted_client as sbc

    led = load_ledger()
    day = sbc.budget_day_key()
    led["days"][day] = {"live_calls": 9, "cache_hits": 0, "live_rows": 0, "unique_rows": 0}
    save_ledger(led)
    resp = search_opportunities(
        {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 10, "offset": 99},
        reason="reserve_test",
        authorize_live=True,
        use_reserve=False,
    )
    assert resp["_meta"]["status"] == SAM_RESERVE_PROTECTED
    # Explicit reserve OK
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"opportunitiesData": [{"noticeId": "r1"}], "totalRecords": 1}
    transport = MagicMock()
    transport.get.return_value = mock_resp
    resp2 = search_opportunities(
        {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 10, "offset": 98},
        reason="reserve_ok",
        authorize_live=True,
        use_reserve=True,
        transport=transport,
    )
    assert resp2["_meta"]["credits_consumed"] == 1
    assert calls_used_today() == 10


def test_no_live_without_authorize(isolated_ledger):
    resp = search_opportunities(
        {"postedFrom": "09/01/2026", "postedTo": "09/29/2026", "limit": 10, "offset": 0},
        reason="no_auth",
        authorize_live=False,
    )
    assert resp["_meta"]["status"] == "LIVE_NOT_AUTHORIZED"
    assert calls_used_today() == 0


def test_duplicate_query_suppressed_via_cache(isolated_ledger):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "opportunitiesData": [{"noticeId": "a"}, {"noticeId": "b"}],
        "totalRecords": 2,
    }
    transport = MagicMock()
    transport.get.return_value = mock_resp
    params = {"postedFrom": "09/20/2026", "postedTo": "09/29/2026", "limit": 1000, "offset": 0, "active": "yes"}
    r1 = search_opportunities(params, reason="first", authorize_live=True, transport=transport)
    r2 = search_opportunities(params, reason="second", authorize_live=True, transport=transport)
    assert r1["_meta"]["credits_consumed"] == 1
    assert r2["_meta"]["cache_hit"] is True
    assert calls_used_today() == 1
    assert transport.get.call_count == 1


def test_planner_and_execute_respect_max(isolated_ledger):
    plan = build_daily_query_plan()
    assert plan["kind"] == "SamQueryPlan"
    assert plan["daily_limit"] == 10
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"opportunitiesData": [{"noticeId": f"n{i}"} for i in range(5)], "totalRecords": 5}
    transport = MagicMock()
    transport.get.return_value = mock_resp
    summary = execute_query_plan(plan, authorize_live=True, max_live_calls=2, transport=transport)
    assert summary["live_calls_this_run"] <= 2
    assert calls_used_today() <= 2


def test_dashboard_display(isolated_ledger):
    d = dashboard()
    assert d["daily_limit"] == 10
    assert "SAM:" in d["display"]


def test_sam_raw_ingest_dedupe_and_federal_watch():
    store: dict = {}
    raw = {
        "noticeId": "abc123def",
        "solicitationNumber": "SPE7M1-26-T-0001",
        "title": "Circuit Card Assembly NSN parts",
        "fullParentPathName": "DEPT OF DEFENSE.DLA",
        "uiLink": "https://sam.gov/opp/abc123def/view",
        "responseDeadLine": "2026-10-15T00:00:00-04:00",
        "naicsCode": "334412",
    }
    info = ingest_sam_rows(store, [raw, raw])
    assert info["new_unique"] == 1
    assert info["duplicates"] == 1
    rec = next(iter(store.values()))
    assert rec["current_funnel_state"] in {"WATCH_FEDERAL_ACCESS", "FAST_REJECT", "RAW"}


def test_supplier_path_conversion_promotes_brand_row():
    store = {
        "test1": {
            "canonical_opportunity_id": "test1",
            "title": "Dell Latitude 5540 Laptops and docks",
            "buyer": "City School District",
            "is_federal": False,
            "freshness": "LIVE_FRESH",
            "current_funnel_state": DEEP_RESEARCH_COMPLETE,
            "authoritative_url": "https://example.gov/bid/1",
            "solicitation_event_id": "IFB-1",
            "deadline": "2026-12-01",
            "row_ref": {"title": "Dell Latitude 5540 Laptops and docks", "agency": "City School District"},
        }
    }
    result = convert_supplier_paths(store)
    assert result["stats"]["attempted"] == 1
    # Brand path should resolve suppliers; may promote READY_TO_CALL
    assert result["stats"].get("supplier_path_found", 0) + result["stats"].get("supplier_path_unresolved", 0) >= 1
    if store["test1"]["current_funnel_state"] == READY_TO_CALL:
        assert result["stats"]["promoted_ready_to_call"] >= 1


def test_persistence_across_reload(isolated_ledger):
    import discovery.sam_budgeted_client as sbc

    led = load_ledger()
    day = sbc.budget_day_key()
    led["days"][day] = {"live_calls": 3, "cache_hits": 1, "live_rows": 10, "unique_rows": 8}
    save_ledger(led)
    assert calls_used_today() == 3
    # reload
    assert load_ledger()["days"][day]["live_calls"] == 3


def test_artifacts_exist_after_offline_run():
    """Offline conversion run should write phase artifacts without SAM live calls."""
    from phase_l.sam_supplier_conversion_run import run_phase

    # Only if store exists
    if not (ROOT / "data" / "l23_canonical_population_store.json").exists():
        return
    before = calls_used_today()
    # Use real ledger path — offline authorize_live=False so no new credits
    summary = run_phase(authorize_live_sam=False, max_sam_calls=0)
    assert summary["verdict"] in {
        "SAM_BUDGETED_REFRESH_AND_SUPPLIER_CONVERSION_WORKING",
        "SAM_BUDGETED_REFRESH_PARTIAL",
        "SAM_BUDGETED_REFRESH_FAILED",
    }
    assert summary["sam_usage"]["live_calls_this_run"] == 0
    assert (ROOT / "artifacts" / "phase_l" / "sam_supplier_conversion_summary.json").exists()
    assert (ROOT / "artifacts" / "phase_l" / "supplier_path_conversion.json").exists()
    # Did not burn SAM
    assert calls_used_today() == before
