"""Euna/Bonfire auth telemetry — no secrets."""

from __future__ import annotations

import json
import logging
from typing import Any

from application_clock import now_utc
from euna_auth.config import load_euna_auth_config
from euna_auth.session_store import storage_state_exists
from euna_auth.states import DISABLED, NOT_CONNECTED

log = logging.getLogger("govtracker.euna_auth.telemetry")
TELEMETRY_FILE = "euna_auth/telemetry.json"


def _empty() -> dict[str, Any]:
    return {
        "kind": "EunaAuthTelemetry",
        "euna_auth_enabled": True,
        "euna_login_attempts": 0,
        "euna_login_successes": 0,
        "euna_login_failures": 0,
        "euna_session_reuses": 0,
        "euna_auth_challenges": 0,
        "euna_last_auth_success": None,
        "euna_last_authenticated_run": None,
        "euna_last_session_reuse": None,
        "euna_last_discovery_run": None,
        "euna_last_failure_reason": None,
        "euna_last_status": NOT_CONNECTED,
        "euna_account_state": None,
        "euna_flow_detected": None,
        "euna_auth_host": None,
        "euna_reported_open": None,
        "euna_harvested": 0,
        "euna_pagination_complete": False,
        "euna_documents_recovered": 0,
        "euna_net_new": 0,
        "credentials_configured": False,
        "storage_state_present": False,
        "updated_at": None,
    }


def _path():
    from m3_data_root import data_path

    return data_path(TELEMETRY_FILE)


def load_telemetry() -> dict[str, Any]:
    base = _empty()
    path = _path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base.update({k: data[k] for k in base if k in data})
        except Exception:
            pass
    cfg = load_euna_auth_config()
    base["euna_auth_enabled"] = cfg.auth_enabled
    base["credentials_configured"] = cfg.credentials_present
    base["storage_state_present"] = storage_state_exists()
    if not cfg.auth_enabled:
        base["euna_last_status"] = base.get("euna_last_status") or DISABLED
    return base


def save_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def record_auth_event(
    status: str,
    *,
    failure_reason: str | None = None,
    login_attempt: bool = False,
    login_success: bool = False,
    login_failure: bool = False,
    session_reuse: bool = False,
    challenge: bool = False,
    account_state: str | None = None,
    flow_detected: str | None = None,
    auth_host: str | None = None,
) -> dict[str, Any]:
    t = load_telemetry()
    t["euna_last_status"] = status
    if login_attempt:
        t["euna_login_attempts"] = int(t.get("euna_login_attempts") or 0) + 1
    if login_success:
        t["euna_login_successes"] = int(t.get("euna_login_successes") or 0) + 1
        t["euna_last_auth_success"] = now_utc().isoformat()
        t["euna_last_failure_reason"] = None
    if login_failure:
        t["euna_login_failures"] = int(t.get("euna_login_failures") or 0) + 1
    if session_reuse:
        t["euna_session_reuses"] = int(t.get("euna_session_reuses") or 0) + 1
        t["euna_last_session_reuse"] = now_utc().isoformat()
    if challenge:
        t["euna_auth_challenges"] = int(t.get("euna_auth_challenges") or 0) + 1
    if failure_reason:
        t["euna_last_failure_reason"] = failure_reason
    if account_state:
        t["euna_account_state"] = account_state
    if flow_detected:
        t["euna_flow_detected"] = flow_detected
    if auth_host:
        t["euna_auth_host"] = auth_host
    return save_telemetry(t)


def record_harvest_counters(payload: dict[str, Any]) -> dict[str, Any]:
    t = load_telemetry()
    for k in (
        "euna_reported_open",
        "euna_harvested",
        "euna_pagination_complete",
        "euna_documents_recovered",
        "euna_net_new",
        "euna_central_reachable",
    ):
        if k in payload:
            t[k] = payload[k]
    t["euna_last_discovery_run"] = now_utc().isoformat()
    t["euna_last_authenticated_run"] = now_utc().isoformat()
    return save_telemetry(t)


def owner_connection_status() -> dict[str, Any]:
    t = load_telemetry()
    cfg = load_euna_auth_config()
    reported = t.get("euna_reported_open")
    harvested = t.get("euna_harvested") or 0
    pagination_pct = None
    if reported:
        try:
            pagination_pct = round(100.0 * float(harvested) / max(1, float(reported)), 2)
        except Exception:
            pagination_pct = None
    from euna_auth.states import PAID_OPTIONAL

    # Owner-facing status: do not mark BROKEN merely because nationwide paid access is off.
    display_status = t.get("euna_last_status") or NOT_CONNECTED
    if not cfg.national_discovery_enabled and not cfg.enabled_states:
        display_status = PAID_OPTIONAL

    return {
        "kind": "EunaConnectionStatus",
        "platform": "Euna/Bonfire",
        "status": display_status,
        "auth_status": t.get("euna_last_status") or NOT_CONNECTED,
        "authenticated": t.get("euna_last_status") in {"CONNECTED", "SESSION_REUSED", "LOGIN_SUCCESS"},
        "euna_auth_enabled": cfg.auth_enabled,
        "national_discovery_enabled": cfg.national_discovery_enabled,
        "enabled_states": list(cfg.enabled_states),
        "enabled_states_display": ",".join(cfg.enabled_states) if cfg.enabled_states else "NONE",
        "source_role": cfg.source_role,
        "coverage_category": cfg.coverage_category,
        "owner_label": "OPTIONAL — PAID STATE ACCESS",
        "credentials_configured": cfg.credentials_present,
        "credential_hygiene": cfg.credential_hygiene(),
        "storage_state_present": storage_state_exists(),
        "discovery_scope": cfg.discovery_scope,
        "account_state": t.get("euna_account_state"),
        "flow_detected": t.get("euna_flow_detected"),
        "auth_host": t.get("euna_auth_host"),
        "last_successful_login": t.get("euna_last_auth_success"),
        "last_session_reuse": t.get("euna_last_session_reuse"),
        "last_discovery_run": t.get("euna_last_discovery_run"),
        "last_authenticated_run": t.get("euna_last_authenticated_run"),
        "last_failure_reason": t.get("euna_last_failure_reason"),
        "reported_open": reported,
        "harvested": harvested,
        "pagination_pct": pagination_pct,
        "pagination_complete": t.get("euna_pagination_complete"),
        "documents_recovered": t.get("euna_documents_recovered"),
        "net_new": t.get("euna_net_new"),
        "central_reachable": t.get("euna_central_reachable"),
        "counts_toward_national_health": False,
        "telemetry": {
            "login_attempts": t.get("euna_login_attempts"),
            "login_successes": t.get("euna_login_successes"),
            "login_failures": t.get("euna_login_failures"),
            "session_reuses": t.get("euna_session_reuses"),
            "auth_challenges": t.get("euna_auth_challenges"),
        },
    }


def startup_health_report() -> None:
    st = owner_connection_status()
    print(
        f"govtracker: Euna auth status={st.get('status')} enabled={st.get('euna_auth_enabled')} "
        f"credentials={st.get('credentials_configured')} session={st.get('storage_state_present')}",
        flush=True,
    )
