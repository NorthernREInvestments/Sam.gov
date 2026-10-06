"""OpenGov auth + discovery telemetry — no secrets."""

from __future__ import annotations

import json
import logging
from typing import Any

from application_clock import now_utc
from opengov_auth.config import load_opengov_auth_config
from opengov_auth.session_store import storage_state_exists
from opengov_auth.states import DISABLED, NOT_CONNECTED

log = logging.getLogger("govtracker.opengov_auth.telemetry")

TELEMETRY_FILE = "opengov_auth/telemetry.json"


def _path():
    from m3_data_root import data_path

    return data_path(TELEMETRY_FILE)


def _empty() -> dict[str, Any]:
    return {
        "kind": "OpenGovAuthTelemetry",
        "opengov_auth_enabled": True,
        "opengov_login_attempts": 0,
        "opengov_login_successes": 0,
        "opengov_login_failures": 0,
        "opengov_session_reuses": 0,
        "opengov_auth_challenges": 0,
        "opengov_last_auth_success": None,
        "opengov_last_authenticated_run": None,
        "opengov_last_session_reuse": None,
        "opengov_last_discovery_run": None,
        "opengov_last_failure_reason": None,
        "opengov_last_status": NOT_CONNECTED,
        "opengov_known_entities": 0,
        "opengov_entities_attempted": 0,
        "opengov_entities_successful": 0,
        "opengov_raw_discovered": 0,
        "opengov_canonical_live": 0,
        "opengov_documents_recovered": 0,
        "opengov_product_candidates": 0,
        "opengov_gov_value_found": 0,
        "opengov_public_cost_found": 0,
        "opengov_economics_ready": 0,
        "OPENGOV_DOCUMENT_RECOVERY_RATE": 0.0,
        "OPENGOV_DETAIL_RECOVERY_RATE": 0.0,
        "OPENGOV_ECONOMICS_READY_RATE": 0.0,
        "account_category_restriction": False,
        "credentials_configured": False,
        "storage_state_present": False,
        "updated_at": None,
    }


def load_telemetry() -> dict[str, Any]:
    path = _path()
    base = _empty()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base.update({k: data[k] for k in base if k in data})
        except Exception:
            pass
    cfg = load_opengov_auth_config()
    base["opengov_auth_enabled"] = cfg.auth_enabled
    base["credentials_configured"] = cfg.credentials_present
    base["storage_state_present"] = storage_state_exists()
    if not cfg.auth_enabled:
        base["opengov_last_status"] = base.get("opengov_last_status") or DISABLED
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
) -> dict[str, Any]:
    t = load_telemetry()
    t["opengov_last_status"] = status
    if login_attempt:
        t["opengov_login_attempts"] = int(t.get("opengov_login_attempts") or 0) + 1
    if login_success:
        t["opengov_login_successes"] = int(t.get("opengov_login_successes") or 0) + 1
        t["opengov_last_auth_success"] = now_utc().isoformat()
        t["opengov_last_failure_reason"] = None
    if login_failure:
        t["opengov_login_failures"] = int(t.get("opengov_login_failures") or 0) + 1
        if failure_reason:
            t["opengov_last_failure_reason"] = _scrub(failure_reason)
    if session_reuse:
        t["opengov_session_reuses"] = int(t.get("opengov_session_reuses") or 0) + 1
        t["opengov_last_session_reuse"] = now_utc().isoformat()
        t["opengov_last_auth_success"] = t.get("opengov_last_auth_success") or now_utc().isoformat()
    if challenge:
        t["opengov_auth_challenges"] = int(t.get("opengov_auth_challenges") or 0) + 1
        t["opengov_last_failure_reason"] = _scrub(failure_reason or "AUTH_CHALLENGE")
    if failure_reason and not login_success:
        t["opengov_last_failure_reason"] = _scrub(failure_reason)
    return save_telemetry(t)


def record_discovery_counters(payload: dict[str, Any]) -> dict[str, Any]:
    t = load_telemetry()
    for key in (
        "opengov_known_entities",
        "opengov_entities_attempted",
        "opengov_entities_successful",
        "opengov_raw_discovered",
        "opengov_harvested",
        "opengov_canonical_live",
        "opengov_documents_recovered",
        "opengov_product_candidates",
        "opengov_gov_value_found",
        "opengov_public_cost_found",
        "opengov_economics_ready",
        "opengov_net_new",
        "opengov_anti_bot_recovered",
        "OPENGOV_DOCUMENT_RECOVERY_RATE",
        "OPENGOV_DETAIL_RECOVERY_RATE",
        "OPENGOV_ECONOMICS_READY_RATE",
    ):
        if key in payload:
            t[key] = payload[key]
    if "opengov_harvested" in payload and "opengov_raw_discovered" not in payload:
        t["opengov_raw_discovered"] = payload["opengov_harvested"]
    if payload.get("account_category_restriction") is not None:
        t["account_category_restriction"] = bool(payload["account_category_restriction"])
    t["opengov_last_discovery_run"] = now_utc().isoformat()
    t["opengov_last_authenticated_run"] = now_utc().isoformat()
    return save_telemetry(t)


def owner_connection_status() -> dict[str, Any]:
    t = load_telemetry()
    cfg = load_opengov_auth_config()
    status = t.get("opengov_last_status") or NOT_CONNECTED
    if not cfg.auth_enabled:
        status = DISABLED
    elif not cfg.credentials_present and status not in {DISABLED}:
        if status in {None, NOT_CONNECTED}:
            status = NOT_CONNECTED
        if not t.get("opengov_last_failure_reason"):
            t = dict(t)
            t["opengov_last_failure_reason"] = "OPENGOV_USERNAME/OPENGOV_PASSWORD not configured"
    return {
        "kind": "OpenGovConnectionStatus",
        "platform": "OpenGov",
        "status": status,
        "authenticated": status in {"CONNECTED", "SESSION_REUSED", "LOGIN_SUCCESS"},
        "opengov_auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "storage_state_present": t.get("storage_state_present"),
        "discovery_scope": cfg.discovery_scope,
        "account_category_restriction": t.get("account_category_restriction"),
        "last_successful_login": t.get("opengov_last_auth_success"),
        "last_session_reuse": t.get("opengov_last_session_reuse"),
        "last_discovery_run": t.get("opengov_last_discovery_run"),
        "last_authenticated_run": t.get("opengov_last_authenticated_run"),
        "last_failure_reason": t.get("opengov_last_failure_reason"),
        "known_entities": t.get("opengov_known_entities") or 0,
        "entities_successful": t.get("opengov_entities_successful") or 0,
        "raw_opportunities": t.get("opengov_raw_discovered") or 0,
        "canonical_live": t.get("opengov_canonical_live") or 0,
        "documents_recovered": t.get("opengov_documents_recovered") or 0,
        "product_candidates": t.get("opengov_product_candidates") or 0,
        "economics_ready": t.get("opengov_economics_ready") or 0,
        "rates": {
            "detail": t.get("OPENGOV_DETAIL_RECOVERY_RATE") or 0,
            "documents": t.get("OPENGOV_DOCUMENT_RECOVERY_RATE") or 0,
            "economics_ready": t.get("OPENGOV_ECONOMICS_READY_RATE") or 0,
        },
        "telemetry": {
            "login_attempts": t.get("opengov_login_attempts") or 0,
            "login_successes": t.get("opengov_login_successes") or 0,
            "login_failures": t.get("opengov_login_failures") or 0,
            "session_reuses": t.get("opengov_session_reuses") or 0,
            "auth_challenges": t.get("opengov_auth_challenges") or 0,
        },
    }


def startup_health_report() -> dict[str, Any]:
    status = owner_connection_status()
    msg = (
        f"govtracker: OpenGov auth status={status.get('status')} "
        f"enabled={status.get('opengov_auth_enabled')} "
        f"credentials={status.get('credentials_configured')} "
        f"session={status.get('storage_state_present')}"
    )
    print(msg, flush=True)
    log.info(
        "OpenGov auth startup status=%s credentials=%s session=%s",
        status.get("status"),
        status.get("credentials_configured"),
        status.get("storage_state_present"),
    )
    return status


def _scrub(text: str) -> str:
    cfg = load_opengov_auth_config()
    out = str(text or "")
    for secret in (cfg.password, cfg.username):
        if secret and len(secret) >= 3 and secret in out:
            out = out.replace(secret, "[REDACTED]")
    return out[:500]
