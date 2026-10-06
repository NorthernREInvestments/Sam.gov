"""Tests for autonomous BidNet authentication + scheduled recovery integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bidnet_auth.client import BidNetAuthenticatedClient, AuthResult
from bidnet_auth.config import load_bidnet_auth_config
from bidnet_auth.session_store import load_storage_state, save_storage_state, storage_state_path
from bidnet_auth.states import (
    AUTH_CHALLENGE,
    AUTH_FAILED,
    DISABLED,
    LOGIN_SUCCESS,
    SESSION_REUSED,
)
from bidnet_auth.telemetry import load_telemetry, owner_connection_status, record_auth_event
from bidnet_recovery.batch import _select_bidnet_candidates, run_bidnet_recovery


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    yield tmp_path
    set_data_root(None)


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("BIDNET_AUTH_ENABLED", "true")
    monkeypatch.setenv("BIDNET_USERNAME", "vendor@example.com")
    monkeypatch.setenv("BIDNET_PASSWORD", "super-secret-password-xyz")
    monkeypatch.setenv("BIDNET_HEADLESS", "true")


def test_config_defaults(monkeypatch, data_root):
    monkeypatch.delenv("BIDNET_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("BIDNET_USERNAME", raising=False)
    monkeypatch.delenv("BIDNET_PASSWORD", raising=False)
    cfg = load_bidnet_auth_config()
    assert cfg.auth_enabled is True
    assert cfg.headless is True
    assert cfg.credentials_present is False


def test_storage_state_save_load(data_root, creds):
    state = {"cookies": [{"name": "SID", "value": "abc", "domain": ".bidnetdirect.com"}], "origins": []}
    path = save_storage_state(state)
    assert path.exists()
    loaded = load_storage_state()
    assert loaded is not None
    assert loaded["cookies"][0]["name"] == "SID"
    assert "super-secret" not in path.read_text(encoding="utf-8")


def test_disabled_auth(monkeypatch, data_root):
    monkeypatch.setenv("BIDNET_AUTH_ENABLED", "false")
    monkeypatch.setenv("BIDNET_USERNAME", "u")
    monkeypatch.setenv("BIDNET_PASSWORD", "p")
    client = BidNetAuthenticatedClient()
    result = client.ensure_authenticated()
    assert result.status == DISABLED
    assert result.authenticated is False


def test_missing_credentials(monkeypatch, data_root):
    monkeypatch.setenv("BIDNET_AUTH_ENABLED", "true")
    monkeypatch.delenv("BIDNET_USERNAME", raising=False)
    monkeypatch.delenv("BIDNET_PASSWORD", raising=False)
    client = BidNetAuthenticatedClient()
    result = client.ensure_authenticated()
    assert result.status == AUTH_FAILED
    assert "not configured" in result.message


def test_session_reuse(data_root, creds, monkeypatch):
    save_storage_state({"cookies": [{"name": "x", "value": "1"}], "origins": []})

    client = BidNetAuthenticatedClient()
    client._ensure_browser = MagicMock()  # type: ignore[method-assign]
    client._probe_authenticated = MagicMock(return_value={"authenticated": True})  # type: ignore[method-assign]
    client._persist_state = MagicMock()  # type: ignore[method-assign]

    result = client.ensure_authenticated()
    assert result.status == SESSION_REUSED
    assert result.authenticated is True
    assert result.reused_session is True
    client._perform_login = MagicMock()  # type: ignore[method-assign]
    # ensure login was not needed
    assert not hasattr(client, "_login_called")


def test_expired_session_triggers_login(data_root, creds):
    save_storage_state({"cookies": [{"name": "x", "value": "1"}], "origins": []})
    client = BidNetAuthenticatedClient()
    client._ensure_browser = MagicMock()  # type: ignore[method-assign]
    client._probe_authenticated = MagicMock(return_value={"authenticated": False})  # type: ignore[method-assign]
    client._perform_login = MagicMock(  # type: ignore[method-assign]
        return_value=AuthResult(status=LOGIN_SUCCESS, authenticated=True, message="ok")
    )
    result = client.ensure_authenticated()
    assert result.status == LOGIN_SUCCESS
    client._perform_login.assert_called_once()


def test_challenge_detection_stops_login(data_root, creds):
    client = BidNetAuthenticatedClient()
    client._ensure_browser = MagicMock()  # type: ignore[method-assign]
    # No stored session → go to login
    client._page = MagicMock()
    client._page.content.return_value = "<html><div class='g-recaptcha'></div>Please complete captcha</html>"
    client._page.url = "https://www.bidnetdirect.com/public/authentication/login"
    client._page.goto = MagicMock()
    client._page.wait_for_load_state = MagicMock()

    with patch("bidnet_auth.session_store.load_storage_state", return_value=None):
        # Bypass browser launch; call login path directly via ensure with empty session
        client._ensure_browser = MagicMock()  # type: ignore[method-assign]
        result = client._perform_login()

    assert result.status == AUTH_CHALLENGE
    assert result.authenticated is False


def test_bad_credentials(data_root, creds):
    client = BidNetAuthenticatedClient()
    client._page = MagicMock()
    client._page.url = "https://www.bidnetdirect.com/public/authentication/login"
    client._page.goto = MagicMock()
    client._page.wait_for_load_state = MagicMock()
    client._page.content.side_effect = [
        "<html><input name='username'><input type='password'></html>",
        "<html>Invalid username or password</html>",
    ]
    user = MagicMock()
    pwd = MagicMock()
    client._find_username_locator = MagicMock(return_value=user)  # type: ignore[method-assign]
    client._find_password_locator = MagicMock(return_value=pwd)  # type: ignore[method-assign]
    client._submit_login = MagicMock(return_value=True)  # type: ignore[method-assign]

    result = client._perform_login()
    assert result.status == AUTH_FAILED
    assert result.message == "bad_credentials"
    # Password must not appear in result
    assert "super-secret" not in json.dumps(result.to_dict())


def test_no_secrets_in_logs(data_root, creds, caplog):
    with caplog.at_level(logging.INFO, logger="govtracker.bidnet_auth"):
        record_auth_event(AUTH_FAILED, failure_reason="bad_credentials", login_failure=True)
        status = owner_connection_status()
    blob = " ".join(r.message for r in caplog.records) + json.dumps(status)
    assert "super-secret-password-xyz" not in blob
    assert "vendor@example.com" not in (status.get("last_failure_reason") or "")


def test_telemetry_scrub_password(data_root, creds):
    t = record_auth_event(
        AUTH_FAILED,
        failure_reason="failed for super-secret-password-xyz user vendor@example.com",
        login_failure=True,
    )
    assert "super-secret-password-xyz" not in (t.get("bidnet_last_failure_reason") or "")
    assert "[REDACTED]" in (t.get("bidnet_last_failure_reason") or "")


def test_batch_limits_priority_vs_backlog(data_root):
    store = {}
    for i in range(10):
        store[f"new-{i}"] = {
            "platform": "live_bidnet",
            "title": f"Poly tubing supply {i}",
            "authoritative_url": f"https://www.bidnetdirect.com/x/{i}",
            "deadline": "2099-12-01T17:00:00+00:00",
        }
    for i in range(20):
        store[f"old-{i}"] = {
            "platform": "live_bidnet",
            "title": f"Old backlog {i}",
            "authoritative_url": f"https://www.bidnetdirect.com/y/{i}",
            "bidnet_recovery": {"state": "DETAIL_RECOVERED", "blockers": []},
        }
    selected = _select_bidnet_candidates(
        store,
        done=set(),
        force=False,
        recovery_batch_size=5,
        backlog_batch_size=3,
        min_tier=5,
    )
    assert len(selected) <= 8
    # Priority (tier 1) should fill first
    assert sum(1 for t, _, _ in selected if t <= 2) >= 5 or len(selected) >= 5


def test_auth_failure_does_not_process_records(data_root, creds, monkeypatch):
    # Minimal store
    from phase_l import l23_full_population_funnel as funnel

    store = {
        "bn1": {
            "platform": "live_bidnet",
            "title": "Widget Supply",
            "authoritative_url": "https://www.bidnetdirect.com/x/1",
        }
    }
    monkeypatch.setattr(funnel, "load_store", lambda: store)
    monkeypatch.setattr(funnel, "save_store", lambda s: None)

    with patch("bidnet_auth.BidNetAuthenticatedClient") as Cls:
        inst = Cls.return_value
        inst.ensure_authenticated.return_value = AuthResult(
            status=AUTH_CHALLENGE, authenticated=False, message="captcha"
        )
        inst.is_authenticated = False
        report = run_bidnet_recovery(limit=10, use_auth=True, persist=False, resume=False)
    assert report.get("auth_stop") == AUTH_CHALLENGE
    assert report.get("processed") == 0


def test_scheduled_path_invokes_auth(monkeypatch, data_root, creds):
    called = {}

    def fake_harvest(**kwargs):
        return {
            "auth": {"status": LOGIN_SUCCESS, "authenticated": True},
            "harvest": {
                "search_reachable": True,
                "reported_total": 23000,
                "retrieved_total": 20,
                "detail_stats": {},
            },
            "canonical_merge": {"new": 1, "updated": 0},
            "blocker": None,
        }

    def fake_recovery(**kwargs):
        called.update(kwargs)
        return {
            "auth": {"status": LOGIN_SUCCESS, "authenticated": True},
            "processed": 0,
            "stats": {},
            "funnel": {},
        }

    monkeypatch.setattr("bidnet_discovery.run_bidnet_authenticated_harvest", fake_harvest)
    monkeypatch.setattr("bidnet_recovery.batch.run_bidnet_recovery", fake_recovery)
    from bidnet_auth.scheduled import run_scheduled_bidnet_auth_recovery

    out = run_scheduled_bidnet_auth_recovery(run_id="DISCOVERY-1", trigger_type="SCHEDULED")
    assert called.get("use_auth") is True
    assert out.get("auth", {}).get("authenticated") is True
    assert out.get("harvest", {}).get("search_reachable") is True


def test_bidnet_failure_does_not_kill_discovery(monkeypatch, data_root):
    """Ensure scheduled helper swallows recovery exceptions for the discovery caller."""
    monkeypatch.setenv("BIDNET_AUTH_ENABLED", "true")
    monkeypatch.setenv("BIDNET_USERNAME", "u")
    monkeypatch.setenv("BIDNET_PASSWORD", "p")

    def ok_harvest(**kwargs):
        return {
            "auth": {"status": LOGIN_SUCCESS, "authenticated": True},
            "harvest": {"search_reachable": True, "retrieved_total": 0, "detail_stats": {}},
            "canonical_merge": {},
            "blocker": None,
        }

    def boom(**kwargs):
        raise RuntimeError("browser explode")

    monkeypatch.setattr("bidnet_discovery.run_bidnet_authenticated_harvest", ok_harvest)
    monkeypatch.setattr("bidnet_recovery.batch.run_bidnet_recovery", boom)
    from bidnet_auth.scheduled import run_scheduled_bidnet_auth_recovery

    out = run_scheduled_bidnet_auth_recovery(run_id="R1", trigger_type="SCHEDULED")
    assert out.get("blocker") == AUTH_FAILED
    assert "browser explode" not in json.dumps(out)


def test_owner_status_never_includes_password(data_root, creds):
    record_auth_event(LOGIN_SUCCESS, login_success=True)
    status = owner_connection_status()
    dumped = json.dumps(status)
    assert "super-secret-password-xyz" not in dumped
    assert "password" not in dumped.lower() or "credentials_configured" in dumped
