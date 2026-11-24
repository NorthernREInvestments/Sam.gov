"""Build 002Q permanent NextBid repair â€” offline mocked tests. ZERO live metered calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from m3.research import nextbid_controller as ctrl
from m3.research.nextbid_killswitch import engage_emergency_stop
from m3.research.nextbid_response_store import (
    load_by_request_id,
    lookup_cached_response,
    merge_thicker,
    persist_paid_response,
    request_identity,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTBID_ENABLED", "false")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "true")
    monkeypatch.setenv("NEXTBID_API_KEY", "test_key_not_real")
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3.config import get_settings

    get_settings.cache_clear()
    engage_emergency_stop(reason="test")
    yield
    get_settings.cache_clear()


def test_durable_persist_before_return(monkeypatch):
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(approved_allocation_calls=1000, authorized=True, note="test")
    ctrl.save_budget_state(
        {
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
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-quota-remaining": "4999", "x-usage-used": "1001", "x-units": "1"}
    mock_resp.text = '{"ok":true,"nb_id":"X1","description":"hello"}'
    mock_resp.json.return_value = {"ok": True, "nb_id": "X1", "description": "hello"}

    with patch.object(ctrl, "_http") as http_factory:
        client = MagicMock()
        client.request.return_value = mock_resp
        http_factory.return_value = client
        data = ctrl.metered_request(
            "GET",
            "/opportunities/X1",
            job_id="job-persist",
            endpoint="get_opportunity",
            nb_id="X1",
            use_cache=False,
        )
    assert data.get("nb_id") == "X1"
    rid = data.get("_request_id") or request_identity(
        "GET", "/opportunities/X1", {}, nb_id="X1"
    )
    stored = load_by_request_id(rid)
    assert stored is not None
    assert stored["body"]["description"] == "hello"


def test_durable_hit_skips_http(monkeypatch):
    persist_paid_response(
        method="GET",
        path="/opportunities/Y1",
        params={},
        endpoint="get_opportunity",
        nb_id="Y1",
        status_code=200,
        body={"ok": True, "nb_id": "Y1", "from_store": True},
        job_id="seed",
    )
    monkeypatch.setenv("NEXTBID_ENABLED", "true")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "false")
    from m3.config import get_settings
    from m3.research.nextbid_killswitch import clear_emergency_stop

    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    ctrl.write_owner_authorization(approved_allocation_calls=1000, authorized=True, note="test")
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "jobs": {},
            "account_remaining": 5000,
            "account_used": 1000,
            "account_allowance": 6000,
            "reconciled_provider_used": 1000,
            "local_metered_since_reconcile": 0,
            "provider_discrepancy_stop": False,
        }
    )
    with patch.object(ctrl, "_http") as http_factory:
        client = MagicMock()
        http_factory.return_value = client
        data = ctrl.metered_request(
            "GET",
            "/opportunities/Y1",
            job_id="job-hit",
            endpoint="get_opportunity",
            nb_id="Y1",
            use_cache=True,
        )
        client.request.assert_not_called()
    assert data.get("from_store") is True
    assert data.get("_durable_store_hit") is True


def test_merge_thicker_never_downgrades():
    out = merge_thicker({"description": "A" * 100, "qty": 5}, {"description": "x", "qty": None})
    assert len(out["description"]) == 100
    assert out["qty"] == 5


def test_persist_retains_history_and_full_body():
    """002R.1 â€” no truncate; historical versions retained; persistence_ok."""
    big = {"nb_id": "HIST1", "description": "D" * 8000, "title": "t1"}
    r1 = persist_paid_response(
        method="GET",
        path="/opportunities/HIST1",
        params={},
        endpoint="get_opportunity",
        nb_id="HIST1",
        status_code=200,
        body=big,
        job_id="hist",
        raw_text="R" * 12000,
    )
    assert r1.get("persistence_ok") is True
    persist_paid_response(
        method="GET",
        path="/opportunities/HIST1",
        params={},
        endpoint="get_opportunity",
        nb_id="HIST1",
        status_code=200,
        body={"nb_id": "HIST1", "description": "short", "title": "t2", "extra": 1},
        job_id="hist2",
    )
    rid = request_identity("GET", "/opportunities/HIST1", {}, nb_id="HIST1")
    stored = load_by_request_id(rid)
    assert stored is not None
    assert len(str((stored.get("body") or {}).get("description") or "")) >= 8000
    assert len(stored.get("historical_versions") or []) >= 1
    hit = lookup_cached_response("GET", "/opportunities/HIST1", {}, nb_id="HIST1")
    assert hit and hit.get("_durable_store_hit") is True


def test_lookup_and_identity():
    a = request_identity("GET", "/opportunities", {"q": "x", "page": 1})
    b = request_identity("GET", "/opportunities", {"page": 1, "q": "x"})
    assert a == b
    persist_paid_response(
        method="GET",
        path="/opportunities",
        params={"q": "x", "page": 1},
        endpoint="search",
        nb_id=None,
        status_code=200,
        body={"ok": True, "results": [{"nb_id": "1"}]},
        job_id="t",
    )
    hit = lookup_cached_response("GET", "/opportunities", {"page": 1, "q": "x"})
    assert hit is not None
    assert hit["_durable_store_hit"] is True


def test_provider_discrepancy_stop():
    ctrl.save_budget_state(
        {
            "day": str(__import__("datetime").date.today()),
            "day_used": 0,
            "jobs": {},
            "account_remaining": 2000,
            "account_used": 3500,
            "account_allowance": 6000,
            "reconciled_provider_used": 3354,
            "local_metered_since_reconcile": 0,
            "provider_discrepancy_stop": False,
        }
    )
    info = ctrl.check_provider_usage_discrepancy(raise_on_stop=False)
    assert info["stop"] is True


def test_locked_blocks_metered():
    with pytest.raises(ctrl.NextBidUnauthorized):
        ctrl.metered_request(
            "GET", "/opportunities", job_id="x", endpoint="search", params={"q": "1"}
        )
