"""Focused tests for M3 automatic production discovery + live status."""

from __future__ import annotations

import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from application_clock import now_utc


@pytest.fixture()
def discovery_env(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("M3_DISCOVERY_INTERVAL_MINUTES", "60")
    monkeypatch.setenv("M3_DISCOVERY_PROFILE", "tiny")
    state_path = tmp_path / "m3_discovery_run_state.json"
    monkeypatch.setattr("m3_discovery_service.DEFAULT_PATH", state_path)
    # Avoid DB side effects for state persistence in unit tests
    monkeypatch.setattr("m3_discovery_service._save_state", lambda state: _file_save(state_path, state))
    monkeypatch.setattr("m3_discovery_service._load_state", lambda: _file_load(state_path))
    yield state_path


def _file_save(path, state):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, default=str), encoding="utf-8")


def _file_load(path):
    import json

    from m3_discovery_service import _empty_state

    if not path.exists():
        return _empty_state()
    return {**_empty_state(), **json.loads(path.read_text(encoding="utf-8"))}


def test_scheduler_enable_disable(monkeypatch, discovery_env):
    from m3_discovery_service import discovery_enabled, discovery_interval_minutes

    assert discovery_enabled() is True
    assert discovery_interval_minutes() == 60
    monkeypatch.setenv("M3_DISCOVERY_ENABLED", "false")
    assert discovery_enabled() is False
    monkeypatch.setenv("M3_DISCOVERY_INTERVAL_MINUTES", "90")
    assert discovery_interval_minutes() == 90


def test_hourly_cadence_calculation(discovery_env):
    from datetime import datetime, timezone

    from m3_discovery_service import compute_next_scheduled_run

    start = now_utc()
    nxt = datetime.fromisoformat(compute_next_scheduled_run(start))
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    delta = nxt - start
    assert 59 * 60 <= delta.total_seconds() <= 61 * 60


def test_startup_stale_and_fresh(monkeypatch, discovery_env):
    from m3_discovery_service import (
        STATUS_COMPLETED,
        _empty_state,
        _save_state,
        is_data_fresh,
        maybe_startup_discovery,
        request_discovery_run,
    )

    # no success → catch-up/startup
    with patch("m3_discovery_service.request_discovery_run") as req:
        req.return_value = {"accepted": True, "queued": True}
        out = maybe_startup_discovery()
        assert out.get("accepted") or out.get("queued") is not False
        assert req.called

    state = _empty_state()
    state["last_successful_completion"] = {
        "completed_at": now_utc().isoformat(),
        "status": STATUS_COMPLETED,
    }
    _save_state(state)
    assert is_data_fresh(state) is True
    with patch("m3_discovery_service.request_discovery_run") as req:
        out = maybe_startup_discovery()
        assert out.get("reason") == "already_fresh"
        assert not req.called

    # stale
    state["last_successful_completion"]["completed_at"] = (now_utc() - timedelta(hours=5)).isoformat()
    _save_state(state)
    assert is_data_fresh() is False


def test_no_overlapping_runs(monkeypatch, discovery_env):
    from m3_discovery_service import STATUS_RUNNING, _empty_state, _save_state, request_discovery_run

    state = _empty_state()
    state["current_run"] = {
        "run_id": "MDR-test",
        "status": STATUS_RUNNING,
        "started_at": now_utc().isoformat(),
        "last_heartbeat_at": now_utc().isoformat(),
        "phase": "DISCOVERING",
        "progress_percent": 20,
    }
    state["lock"] = {"held": True, "run_id": "MDR-test", "since": now_utc().isoformat()}
    _save_state(state)

    out = request_discovery_run(trigger_type="MANUAL")
    assert out["accepted"] is False
    assert out["already_running"] is True
    assert "already running" in (out.get("message") or "").lower()


def test_stale_lock_recovery(discovery_env):
    from m3_discovery_service import STATUS_RUNNING, STATUS_STALE_RECOVERED, _empty_state, _save_state, recover_stale_runs

    state = _empty_state()
    state["current_run"] = {
        "run_id": "MDR-stale",
        "status": STATUS_RUNNING,
        "started_at": (now_utc() - timedelta(hours=2)).isoformat(),
        "last_heartbeat_at": (now_utc() - timedelta(hours=1)).isoformat(),
        "phase": "DISCOVERING",
        "progress_percent": 12,
    }
    state["lock"] = {"held": True, "run_id": "MDR-stale", "since": (now_utc() - timedelta(hours=2)).isoformat()}
    _save_state(state)
    recovered = recover_stale_runs()
    assert recovered.get("current_run") is None
    assert recovered["last_attempt"]["status"] == STATUS_STALE_RECOVERED
    assert recovered["lock"]["held"] is False


def test_progress_monotonic_and_real(discovery_env):
    from m3_discovery_service import _progress_for_phase

    p1 = _progress_for_phase("DISCOVERING", 0, 10)
    p2 = _progress_for_phase("DISCOVERING", 5, 10)
    p3 = _progress_for_phase("CHEAP_SCREENING", 10, 10)
    assert p1 < p2 < p3
    assert _progress_for_phase("FINALIZING", 10, 10) >= 95


def test_failed_source_checkpoint_protection(tmp_path):
    from procurement_source_registry import ProcurementSourceRegistry

    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.record_attempt("src_a", attempted_checkpoint="t1", success=True, successful_checkpoint="SUCCESS-1")
    ok = reg.get("src_a")["last_successful_checkpoint"]
    reg.record_attempt("src_a", attempted_checkpoint="t2", success=False, failure_class="TIMEOUT")
    assert reg.get("src_a")["last_successful_checkpoint"] == ok


def test_freshness_based_on_last_successful(discovery_env):
    from m3_discovery_service import STATUS_COMPLETED, STATUS_FAILED, _empty_state, _save_state, discovery_status

    state = _empty_state()
    state["last_successful_completion"] = {
        "completed_at": (now_utc() - timedelta(minutes=10)).isoformat(),
        "status": STATUS_COMPLETED,
        "product_screen_survivors": 3,
        "pipeline_new": 1,
        "pipeline_updated": 0,
    }
    state["last_attempt"] = {
        "completed_at": now_utc().isoformat(),
        "status": STATUS_FAILED,
        "error_summary": "boom",
    }
    _save_state(state)
    st = discovery_status()
    assert st["last_successful_completion"]["completed_at"] != st["last_attempt"]["completed_at"]
    assert st["status"] == "FAILED"


def test_status_and_run_api(monkeypatch):
    monkeypatch.setattr("app.auth_enabled", lambda: False)
    monkeypatch.setattr("auth.auth_enabled", lambda: False)
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    r = client.get("/api/m3/discovery/status")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "M3DiscoveryStatus"
    assert "progress_percent" in body
    assert body.get("commercial_outreach") is False

    with patch("m3_discovery_service._execute_run"):
        with patch("m3_discovery_service.threading.Thread") as th:
            th.return_value = MagicMock()
            with patch("m3_discovery_service.recover_stale_runs") as rec:
                from m3_discovery_service import _empty_state

                rec.return_value = _empty_state()
                post = client.post("/api/m3/discovery/run")
                assert post.status_code == 200
                assert post.json().get("accepted") in {True, False}


def test_pipeline_handoff_counts_created():
    from m3_discovery_service import _records_from_live_result

    live = {
        "handoff_records": [
            {
                "title": "Laptops",
                "external_id": "X-1",
                "status": "OPEN",
                "source_id": "demo",
                "product_classification": "CORE_PRODUCT",
            }
        ]
    }
    recs = _records_from_live_result(live)
    assert len(recs) == 1
    assert recs[0]["external_id"] == "X-1"


def test_development_no_outreach_preserved(monkeypatch):
    monkeypatch.delenv("APP_EMAIL", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from fastapi.testclient import TestClient

    from app import app
    from m3_discovery_service import discovery_status

    st = discovery_status()
    assert st.get("DEVELOPMENT_NO_OUTREACH") is True
    assert st.get("commercial_outreach") is False
    client = TestClient(app)
    health = client.get("/api/m3/health").json()
    assert health.get("DEVELOPMENT_NO_OUTREACH") is True
    assert health.get("external_action_safety", {}).get("emails_sent") == 0


def test_mobile_dashboard_includes_discovery():
    from m3_mobile_read_model import mobile_dashboard_summary
    from m3_pipeline_store import M3PipelineStore
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = M3PipelineStore(path=Path(td) / "pipe.json")
        body = mobile_dashboard_summary(store)
    assert "discovery" in body
    assert body["discovery"].get("kind") == "M3DiscoveryStatus"


def test_ui_has_discovery_status_markup():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="m3-discovery-status"' in html
    assert "m3-discovery-run-now" in js
    assert "/api/m3/discovery/run" in js
    assert "/api/m3/discovery/status" in js
