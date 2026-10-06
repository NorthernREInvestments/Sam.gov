"""Tests for OpenGov authenticated discovery + recovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opengov_auth.client import AuthResult, OpenGovAuthenticatedClient
from opengov_auth.config import M3_DISCOVERY_SCOPE, load_opengov_auth_config
from opengov_auth.session_store import load_storage_state, save_storage_state
from opengov_auth.states import (
    AUTH_CHALLENGE,
    AUTH_FAILED,
    DISABLED,
    LOGIN_SUCCESS,
    NO_OPEN_BIDS,
    BROKEN_PARSER,
    MOVED,
    SESSION_REUSED,
    WORKING,
)
from opengov_auth.telemetry import owner_connection_status, record_auth_event
from opengov_discovery.parse import parse_detail_html, parse_opengov_portal_html
from opengov_discovery.portals import classify_portal_fetch, is_opengov_url
from opengov_recovery.recover import recover_one


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opengov_portal_sample.html"


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    yield tmp_path
    set_data_root(None)


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("OPENGOV_AUTH_ENABLED", "true")
    monkeypatch.setenv("OPENGOV_USERNAME", "vendor@example.com")
    monkeypatch.setenv("OPENGOV_PASSWORD", "opengov-secret-password-xyz")
    monkeypatch.setenv("OPENGOV_HEADLESS", "true")


def test_config_scope_is_broad(monkeypatch, data_root):
    cfg = load_opengov_auth_config()
    assert cfg.discovery_scope == M3_DISCOVERY_SCOPE
    assert cfg.discovery_scope == "BROAD_PRODUCT_RESALE"


def test_storage_state_save_load(data_root, creds):
    state = {"cookies": [{"name": "OG", "value": "1", "domain": ".opengov.com"}], "origins": []}
    save_storage_state(state)
    loaded = load_storage_state()
    assert loaded is not None
    assert loaded["cookies"][0]["name"] == "OG"


def test_disabled_auth(monkeypatch, data_root):
    monkeypatch.setenv("OPENGOV_AUTH_ENABLED", "false")
    monkeypatch.setenv("OPENGOV_USERNAME", "u")
    monkeypatch.setenv("OPENGOV_PASSWORD", "p")
    result = OpenGovAuthenticatedClient().ensure_authenticated()
    assert result.status == DISABLED
    assert result.authenticated is False


def test_missing_credentials(monkeypatch, data_root):
    monkeypatch.setenv("OPENGOV_AUTH_ENABLED", "true")
    monkeypatch.delenv("OPENGOV_USERNAME", raising=False)
    monkeypatch.delenv("OPENGOV_PASSWORD", raising=False)
    result = OpenGovAuthenticatedClient().ensure_authenticated()
    assert result.status == AUTH_FAILED


def test_session_reuse(data_root, creds):
    save_storage_state({"cookies": [{"name": "x", "value": "1"}], "origins": []})
    client = OpenGovAuthenticatedClient()
    client._ensure_browser = MagicMock()  # type: ignore[method-assign]
    client._probe_authenticated = MagicMock(return_value={"authenticated": True})  # type: ignore[method-assign]
    client._persist_state = MagicMock()  # type: ignore[method-assign]
    result = client.ensure_authenticated()
    assert result.status == SESSION_REUSED
    assert result.reused_session is True


def test_expired_session_triggers_login(data_root, creds):
    save_storage_state({"cookies": [{"name": "x", "value": "1"}], "origins": []})
    client = OpenGovAuthenticatedClient()
    client._ensure_browser = MagicMock()  # type: ignore[method-assign]
    client._probe_authenticated = MagicMock(return_value={"authenticated": False})  # type: ignore[method-assign]
    client._perform_login = MagicMock(  # type: ignore[method-assign]
        return_value=AuthResult(status=LOGIN_SUCCESS, authenticated=True, message="ok")
    )
    result = client.ensure_authenticated()
    assert result.status == LOGIN_SUCCESS
    client._perform_login.assert_called_once()


def test_challenge_detection(data_root, creds):
    client = OpenGovAuthenticatedClient()
    client._page = MagicMock()
    client._page.url = "https://procurement.opengov.com/login"
    client._page.goto = MagicMock()
    client._page.wait_for_load_state = MagicMock()
    client._page.content.return_value = "<html><div class='g-recaptcha'></div>Please complete captcha</html>"
    with patch("opengov_auth.session_store.load_storage_state", return_value=None):
        client._ensure_browser = MagicMock()  # type: ignore[method-assign]
        result = client._perform_login()
    assert result.status == AUTH_CHALLENGE


def test_bad_credentials(data_root, creds):
    client = OpenGovAuthenticatedClient()
    client._page = MagicMock()
    client._page.url = "https://procurement.opengov.com/login"
    client._page.goto = MagicMock()
    client._page.wait_for_load_state = MagicMock()
    client._page.wait_for_timeout = MagicMock()
    client._page.inner_text.return_value = "Invalid username or password"
    client._page.title.return_value = "Login"
    client._page.frames = []
    client._page.content.return_value = "<html><input name='email'><input type='password'>Invalid username or password</html>"
    client._page.get_by_role = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    client._page.get_by_label = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    client._page.get_by_placeholder = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    user = MagicMock()
    pw = MagicMock()
    client._find_username_locator = MagicMock(return_value=user)  # type: ignore[method-assign]
    client._find_password_locator = MagicMock(return_value=pw)  # type: ignore[method-assign]
    client._submit_login = MagicMock(return_value=True)  # type: ignore[method-assign]
    result = client._perform_login()
    assert result.status == AUTH_FAILED
    assert result.message == "bad_credentials"
    assert "opengov-secret" not in json.dumps(result.to_dict())
    user.fill.assert_called_once()
    pw.fill.assert_called_once()


def test_multistep_vendor_login_email_then_password(data_root, creds):
    """Vendor Login → email → Continue → password → Login (real OpenGov flow)."""
    client = OpenGovAuthenticatedClient()
    client._page = MagicMock()
    # After submit, URL leaves /login so post-wait exits quickly
    client._page.url = "https://procurement.opengov.com/login"
    client._page.goto = MagicMock()
    client._page.wait_for_load_state = MagicMock()

    def _timeout(_ms):
        client._page.url = "https://procurement.opengov.com/vendor/dashboard"

    client._page.wait_for_timeout = MagicMock(side_effect=_timeout)
    client._page.title.return_value = "Vendor Login"
    client._page.frames = []
    client._page.content.return_value = "<html>Welcome to vendor dashboard</html>"
    client._context = MagicMock()
    client._context.pages = [client._page]

    email_loc = MagicMock()
    pass_loc = MagicMock()
    # email appears after Vendor Login; password only after Continue
    client._find_username_locator = MagicMock(side_effect=[None, email_loc, email_loc])  # type: ignore[method-assign]
    client._find_password_locator = MagicMock(side_effect=[None, pass_loc, None, None])  # type: ignore[method-assign]
    client._click_vendor_login = MagicMock(return_value=True)  # type: ignore[method-assign]
    client._click_continue = MagicMock(return_value=True)  # type: ignore[method-assign]
    client._submit_login = MagicMock(return_value=True)  # type: ignore[method-assign]
    client._probe_authenticated = MagicMock(return_value={"authenticated": True})  # type: ignore[method-assign]
    client._persist_state = MagicMock()  # type: ignore[method-assign]
    client._page.get_by_role = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    client._page.get_by_label = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))
    client._page.get_by_placeholder = MagicMock(return_value=MagicMock(count=MagicMock(return_value=0)))

    result = client._perform_login()
    assert result.status == LOGIN_SUCCESS
    assert result.authenticated is True
    client._click_vendor_login.assert_called_once()
    client._click_continue.assert_called_once()
    email_loc.fill.assert_called_once()
    pass_loc.fill.assert_called_once()
    assert result.details.get("real_login_flow") == "vendor_login_email_continue_password"
    assert "opengov-secret" not in json.dumps(result.to_dict())


def test_no_secrets_in_logs(data_root, creds, caplog):
    with caplog.at_level(logging.INFO, logger="govtracker.opengov_auth"):
        record_auth_event(AUTH_FAILED, failure_reason="bad for opengov-secret-password-xyz", login_failure=True)
        status = owner_connection_status()
    blob = " ".join(r.message for r in caplog.records) + json.dumps(status)
    assert "opengov-secret-password-xyz" not in blob


def test_portal_family_detection():
    assert is_opengov_url("https://procurement.opengov.com/portal/raleigh")
    assert not is_opengov_url("https://www.bidnetdirect.com/x")


def test_classify_broken_parser_vs_no_bids():
    assert (
        classify_portal_fetch(
            url="https://procurement.opengov.com/portal/x",
            final_url="https://procurement.opengov.com/portal/x",
            html="<html>projectTitle proposalDeadline /projects/ OpenGov</html>",
            n_opps=0,
            parse_signals=False,
        )
        == BROKEN_PARSER
    )
    assert (
        classify_portal_fetch(
            url="https://procurement.opengov.com/portal/x",
            final_url="https://procurement.opengov.com/portal/x",
            html="<html>There are no open solicitations at this time.</html>",
            n_opps=0,
            parse_signals=False,
        )
        == NO_OPEN_BIDS
    )


def test_classify_moved_portal():
    assert (
        classify_portal_fetch(
            url="https://procurement.opengov.com/portal/old",
            final_url="https://www.example.gov/purchasing",
            html="<html>Purchasing</html>",
            n_opps=0,
            parse_signals=False,
        )
        == MOVED
    )


def test_parse_fixture_pagination_fields():
    html = FIXTURE.read_text(encoding="utf-8")
    recs = parse_opengov_portal_html(
        html,
        list_url="https://procurement.opengov.com/portal/raleigh",
        agency="City of Raleigh",
    )
    assert len(recs) >= 2
    titles = " ".join(r.get("title") or "" for r in recs)
    assert "Traffic Signal" in titles or "Police Vehicle" in titles
    # Documents from JSON
    with_docs = [r for r in recs if r.get("document_links")]
    assert with_docs


def test_detail_recovery_fixture(data_root):
    html = """
    <html><title>Widget Supply - OpenGov</title><h1>Widget Supply Contract</h1>
    <div>Proposal Deadline:</div><div>December 1, 2026 5:00 PM EST</div>
    <div>Organization:</div><div>City of Test</div>
    <div>Solicitation Number:</div><div>RFQ-99</div>
    <div>Description</div><p>Purchase of industrial widgets and spare parts for municipal inventory.</p>
    <a href="/files/pricing.xlsx">Pricing Sheet</a>
    <a href="/files/specs.pdf">Specifications</a>
    </html>
    """
    rec = {
        "platform": "live_opengov",
        "title": "Widget Supply Contract",
        "authoritative_url": "https://procurement.opengov.com/portal/x/projects/1",
    }
    out = recover_one("og1", rec, html_fixture=html, fetch_live=False)
    assert out.get("ok")
    assert out.get("improved") or out.get("deadline_recovered") or out.get("documents")
    assert rec.get("opengov_recovery", {}).get("state") in {
        "DETAIL_RECOVERED",
        "DOCUMENTS_RECOVERED",
        "PRODUCT_IDENTIFIED",
        "NOT_PRODUCT",
    }


def test_discovery_scope_not_limited_by_vendor_prefs(data_root, creds):
    from opengov_discovery.harvest import _to_record

    rec = _to_record(
        {"title": "X", "external_id": "1", "detail_url": "https://procurement.opengov.com/p/1"},
        source_id="opengov_test",
    )
    assert rec["discovery_scope"] == "BROAD_PRODUCT_RESALE"
    assert rec["raw_metadata"]["vendor_profile_codes_ignored"] is True


def test_scheduled_path_invokes_opengov(monkeypatch, data_root, creds):
    called = {}

    def fake_disc(**kwargs):
        called["discovery"] = kwargs
        return {
            "auth": {"status": LOGIN_SUCCESS, "authenticated": True},
            "entities_attempted": 0,
            "raw_opportunities": 0,
            "unique_records": 0,
            "canonical_merge": {},
        }

    def fake_rec(**kwargs):
        called["recovery"] = kwargs
        return {"processed": 0, "stats": {}, "funnel": {}}

    monkeypatch.setattr("opengov_discovery.harvest.run_opengov_authenticated_discovery", fake_disc)
    monkeypatch.setattr("opengov_recovery.batch.run_opengov_recovery", fake_rec)
    from opengov_auth.scheduled import run_scheduled_opengov_auth_pipeline

    out = run_scheduled_opengov_auth_pipeline(run_id="D1", trigger_type="SCHEDULED")
    assert called["discovery"].get("use_auth") is True
    assert called["recovery"].get("use_auth") is True
    assert out.get("discovery") is not None


def test_opengov_failure_does_not_raise(monkeypatch, data_root, creds):
    def boom(**kwargs):
        raise RuntimeError("browser explode")

    monkeypatch.setattr("opengov_discovery.harvest.run_opengov_authenticated_discovery", boom)
    from opengov_auth.scheduled import run_scheduled_opengov_auth_pipeline

    out = run_scheduled_opengov_auth_pipeline(run_id="R1", trigger_type="SCHEDULED")
    assert out.get("blocker") == AUTH_FAILED
    assert "browser explode" not in json.dumps(out)


def test_auth_failure_stops_discovery_cleanly(data_root, creds, monkeypatch):
    class _Fake:
        def ensure_authenticated(self):
            return AuthResult(status=AUTH_CHALLENGE, authenticated=False, message="captcha")

        def close(self):
            pass

    monkeypatch.setattr(
        "opengov_auth.OpenGovAuthenticatedClient",
        lambda *a, **k: _Fake(),
    )
    from opengov_discovery.harvest import run_opengov_authenticated_discovery

    report = run_opengov_authenticated_discovery(max_entities=5, persist=False, use_auth=True)
    assert report.get("blocker") == AUTH_CHALLENGE
    assert report.get("raw_opportunities", 0) == 0


def test_working_status_when_opps_found():
    assert (
        classify_portal_fetch(
            url="https://procurement.opengov.com/portal/x",
            final_url="https://procurement.opengov.com/portal/x",
            html="<html>projects</html>",
            n_opps=5,
            parse_signals=True,
        )
        == WORKING
    )


def test_execute_run_contains_opengov_hook():
    import inspect
    from m3_discovery_service import _execute_run

    src = inspect.getsource(_execute_run)
    assert "run_scheduled_opengov_auth_pipeline" in src
    assert "run_scheduled_bidnet_auth_recovery" in src
