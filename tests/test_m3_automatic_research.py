"""Focused tests for M3 automatic research queue + Home progress."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from application_clock import now_utc


@pytest.fixture()
def research_env(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("M3_RESEARCH_INTERVAL_MINUTES", "5")
    state_path = tmp_path / "m3_research_run_state.json"
    monkeypatch.setattr("m3_research_service.DEFAULT_PATH", state_path)
    monkeypatch.setattr("m3_research_service._save_state", lambda state: _file_save(state_path, state))
    monkeypatch.setattr("m3_research_service._load_state", lambda: _file_load(state_path))
    monkeypatch.setattr("m3_research_service._load_state_from_db", lambda: None)
    yield state_path


def _file_save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, default=str), encoding="utf-8")


def _file_load(path):
    from m3_research_service import _empty_state

    if not path.exists():
        return _empty_state()
    return {**_empty_state(), **json.loads(path.read_text(encoding="utf-8"))}


def test_research_enabled_interval(monkeypatch, research_env):
    from m3_research_service import research_enabled, research_interval_minutes

    assert research_enabled() is True
    assert research_interval_minutes() == 5
    monkeypatch.setenv("M3_RESEARCH_ENABLED", "false")
    assert research_enabled() is False


def test_needs_research_idempotent_fingerprint(research_env):
    from m3_research_service import _needs_research

    row = {
        "canonical_id": "sol:a",
        "lifecycle": "RESEARCH_QUEUED",
        "pending_next_action": {"next_action": "QUEUE_RESEARCH"},
        "evidence_fingerprint": "fp1",
        "research_completed_fingerprint": "fp1",
        "research_last_attempt_at": now_utc().isoformat(),
    }
    assert _needs_research(row) is False

    row2 = dict(row)
    row2["research_completed_fingerprint"] = None
    assert _needs_research(row2) is True

    row3 = dict(row)
    row3["evidence_fingerprint"] = "fp2"
    assert _needs_research(row3) is True


def test_classify_outcome(research_env):
    from m3_research_service import _classify_outcome

    before = {"lifecycle": "RESEARCH_QUEUED"}
    assert (
        _classify_outcome(before, {"lifecycle": "REJECTED", "rejected": True}) == "rejected"
    )
    assert (
        _classify_outcome(
            before,
            {
                "lifecycle": "BOM_READY",
                "pending_next_action": {"next_action": "AUTO_CONTINUE"},
            },
        )
        == "advanced"
    )
    assert (
        _classify_outcome(
            before,
            {
                "lifecycle": "RESEARCH_QUEUED",
                "pending_next_action": {"next_action": "WAIT_PACKAGE"},
            },
        )
        == "deferred"
    )
    assert _classify_outcome(before, None, error="boom") == "failed"


def test_orphan_lock_cleared_on_boot(research_env):
    from m3_research_service import (
        STATUS_RUNNING,
        STATUS_STALE_RECOVERED,
        _empty_state,
        _save_state,
        clear_orphan_locks_on_boot,
    )

    state = _empty_state()
    state["current_run"] = {
        "run_id": "MRR-orphan",
        "status": STATUS_RUNNING,
        "started_at": now_utc().isoformat(),
        "heartbeat_at": now_utc().isoformat(),
    }
    state["lock"] = {"held": True, "run_id": "MRR-orphan", "since": now_utc().isoformat()}
    _save_state(state)
    out = clear_orphan_locks_on_boot()
    assert out.get("current_run") is None
    assert out["last_attempt"]["status"] == STATUS_STALE_RECOVERED
    assert out["lock"]["held"] is False


def test_request_run_no_candidates(research_env):
    from m3_research_service import request_research_run

    with patch("m3_research_service.list_research_candidates", return_value=[]):
        out = request_research_run()
        assert out["accepted"] is False
        assert out["reason"] == "no_candidates"


def test_request_run_dispatches_worker(research_env):
    from m3_research_service import request_research_run

    fake = [
        {
            "canonical_id": "sol:1",
            "title": "Widget Supply",
            "lifecycle": "RESEARCH_QUEUED",
            "pending_next_action": {"next_action": "QUEUE_RESEARCH"},
            "evidence_fingerprint": "abc",
        }
    ]
    with patch("m3_research_service.list_research_candidates", return_value=fake):
        with patch("m3_research_service._dispatch_research_job") as disp:
            out = request_research_run()
            assert out["accepted"] is True
            assert out["run_id"].startswith("MRR-")
            assert disp.called


def test_progress_percent_from_processed(research_env):
    from m3_research_service import _empty_state, _save_state, _update_run, research_status

    state = _empty_state()
    state["current_run"] = {
        "run_id": "MRR-prog",
        "status": "RUNNING",
        "started_at": now_utc().isoformat(),
        "heartbeat_at": now_utc().isoformat(),
        "total_candidates": 10,
        "processed": 0,
        "rejected": 0,
        "advanced": 0,
        "deferred": 0,
        "failed": 0,
        "queued": 10,
        "throughput_seconds": [],
        "processed_ids": [],
        "progress_percent": 0,
    }
    state["lock"] = {"held": True, "run_id": "MRR-prog", "since": now_utc().isoformat()}
    _save_state(state)
    _update_run("MRR-prog", processed=4, rejected=1, advanced=1, deferred=2)
    with patch("m3_research_service.list_research_candidates", return_value=[{}] * 6):
        st = research_status()
    assert st["running"] is True
    assert st["progress_percent"] == 40
    assert st["current_run"]["eta_label"] == "Estimating..."


def test_eta_after_enough_samples(research_env):
    from m3_research_service import _empty_state, _save_state, _update_run

    state = _empty_state()
    state["current_run"] = {
        "run_id": "MRR-eta",
        "status": "RUNNING",
        "started_at": now_utc().isoformat(),
        "heartbeat_at": now_utc().isoformat(),
        "total_candidates": 10,
        "processed": 3,
        "queued": 7,
        "throughput_seconds": [10.0, 12.0, 8.0],
        "processed_ids": ["a", "b", "c"],
        "progress_percent": 30,
        "rejected": 0,
        "advanced": 0,
        "deferred": 3,
        "failed": 0,
    }
    _save_state(state)
    _update_run("MRR-eta")
    from m3_research_service import _load_state

    cur = _load_state()["current_run"]
    assert cur["eta_seconds"] is not None
    assert cur["eta_label"] != "Estimating..."


def test_api_research_status(monkeypatch):
    monkeypatch.setattr("app.auth_enabled", lambda: False)
    monkeypatch.setattr("auth.auth_enabled", lambda: False)
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    with patch("m3_research_service.list_research_candidates", return_value=[]):
        r = client.get("/api/m3/research/status")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "M3ResearchStatus"
    assert "progress_percent" in body
    assert "display" in body


def test_ui_has_research_status_placeholder():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="m3-research-status"' in html
    assert "Loading research status" in html
    assert "/api/m3/research/status" in js
    assert "/api/m3/research/run" in js
    assert "renderResearchStatus" in js


def test_health_mentions_research(monkeypatch):
    monkeypatch.setattr("app.auth_enabled", lambda: False)
    monkeypatch.setattr("auth.auth_enabled", lambda: False)
    from fastapi.testclient import TestClient

    from app import APP_BUILD_VERSION, app

    client = TestClient(app)
    r = client.get("/api/m3/health")
    assert r.status_code == 200
    body = r.json()
    assert body["build_version"] == APP_BUILD_VERSION
    assert "research" in body
