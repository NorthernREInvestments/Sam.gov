"""BidNet auth telemetry — no secrets."""

from __future__ import annotations

import json
import logging
from typing import Any

from application_clock import now_utc
from bidnet_auth.config import load_bidnet_auth_config
from bidnet_auth.session_store import storage_state_exists
from bidnet_auth.states import DISABLED

log = logging.getLogger("govtracker.bidnet_auth.telemetry")

TELEMETRY_FILE = "bidnet_auth/telemetry.json"


def _path():
    from m3_data_root import data_path

    return data_path(TELEMETRY_FILE)


def _empty() -> dict[str, Any]:
    return {
        "kind": "BidNetAuthTelemetry",
        "bidnet_auth_enabled": True,
        "bidnet_login_attempts": 0,
        "bidnet_login_successes": 0,
        "bidnet_login_failures": 0,
        "bidnet_session_reuses": 0,
        "bidnet_auth_challenges": 0,
        "bidnet_last_auth_success": None,
        "bidnet_last_authenticated_run": None,
        "bidnet_last_session_reuse": None,
        "bidnet_last_failure_reason": None,
        "bidnet_last_status": None,
        "bidnet_authenticated_detail_recovered": 0,
        "bidnet_documents_recovered": 0,
        "bidnet_economics_ready": 0,
        "bidnet_reported_open": None,
        "bidnet_last_harvested": 0,
        "bidnet_pagination_pct": None,
        "bidnet_pagination_complete": False,
        "bidnet_discovery_truncated": False,
        "bidnet_net_new_unique": 0,
        "bidnet_existing_enriched": 0,
        "bidnet_detail_failure_counts": {},
        "bidnet_last_harvest_at": None,
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
    cfg = load_bidnet_auth_config()
    base["bidnet_auth_enabled"] = cfg.auth_enabled
    base["credentials_configured"] = cfg.credentials_present
    base["storage_state_present"] = storage_state_exists()
    if not cfg.auth_enabled:
        base["bidnet_last_status"] = base.get("bidnet_last_status") or DISABLED
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
    t["bidnet_last_status"] = status
    if login_attempt:
        t["bidnet_login_attempts"] = int(t.get("bidnet_login_attempts") or 0) + 1
    if login_success:
        t["bidnet_login_successes"] = int(t.get("bidnet_login_successes") or 0) + 1
        t["bidnet_last_auth_success"] = now_utc().isoformat()
        t["bidnet_last_failure_reason"] = None
    if login_failure:
        t["bidnet_login_failures"] = int(t.get("bidnet_login_failures") or 0) + 1
        if failure_reason:
            t["bidnet_last_failure_reason"] = _scrub(failure_reason)
    if session_reuse:
        t["bidnet_session_reuses"] = int(t.get("bidnet_session_reuses") or 0) + 1
        t["bidnet_last_session_reuse"] = now_utc().isoformat()
        t["bidnet_last_auth_success"] = t.get("bidnet_last_auth_success") or now_utc().isoformat()
    if challenge:
        t["bidnet_auth_challenges"] = int(t.get("bidnet_auth_challenges") or 0) + 1
        t["bidnet_last_failure_reason"] = _scrub(failure_reason or "AUTH_CHALLENGE")
    if failure_reason and not login_success:
        t["bidnet_last_failure_reason"] = _scrub(failure_reason)
    return save_telemetry(t)


def record_harvest_counters(
    *,
    reported_total: int | None = None,
    retrieved_total: int | None = None,
    pages_scanned: int | None = None,
    pagination_complete: bool = False,
    discovery_truncated: bool = False,
    detail_stats: dict[str, Any] | None = None,
    detail_failure_counts: dict[str, Any] | None = None,
    net_new: int = 0,
    enriched: int = 0,
) -> dict[str, Any]:
    t = load_telemetry()
    if reported_total is not None:
        t["bidnet_reported_open"] = int(reported_total)
    if retrieved_total is not None:
        t["bidnet_last_harvested"] = int(retrieved_total)
    if pages_scanned is not None:
        t["bidnet_pages_scanned"] = int(pages_scanned)
    t["bidnet_pagination_complete"] = bool(pagination_complete)
    t["bidnet_discovery_truncated"] = bool(discovery_truncated)
    if reported_total and retrieved_total is not None and int(reported_total) > 0:
        t["bidnet_pagination_pct"] = round(100.0 * int(retrieved_total) / int(reported_total), 2)
    t["bidnet_net_new_unique"] = int(t.get("bidnet_net_new_unique") or 0) + int(net_new or 0)
    t["bidnet_existing_enriched"] = int(t.get("bidnet_existing_enriched") or 0) + int(enriched or 0)
    if detail_failure_counts:
        t["bidnet_detail_failure_counts"] = dict(detail_failure_counts)
    if detail_stats:
        t["bidnet_last_detail_stats"] = dict(detail_stats)
    t["bidnet_last_harvest_at"] = now_utc().isoformat()
    t["bidnet_last_authenticated_run"] = now_utc().isoformat()
    return save_telemetry(t)


def record_recovery_counters(
    *,
    detail_recovered: int = 0,
    documents_recovered: int = 0,
    economics_ready: int = 0,
    authenticated_run: bool = False,
) -> dict[str, Any]:
    t = load_telemetry()
    t["bidnet_authenticated_detail_recovered"] = int(t.get("bidnet_authenticated_detail_recovered") or 0) + int(
        detail_recovered
    )
    t["bidnet_documents_recovered"] = int(t.get("bidnet_documents_recovered") or 0) + int(documents_recovered)
    t["bidnet_economics_ready"] = int(t.get("bidnet_economics_ready") or 0) + int(economics_ready)
    if authenticated_run:
        t["bidnet_last_authenticated_run"] = now_utc().isoformat()
    return save_telemetry(t)


def owner_connection_status() -> dict[str, Any]:
    """Safe payload for Settings → Connections (no secrets)."""
    t = load_telemetry()
    cfg = load_bidnet_auth_config()
    status = t.get("bidnet_last_status")
    if not cfg.auth_enabled:
        status = DISABLED
    elif not cfg.credentials_present and status not in {DISABLED}:
        status = status or "FAILED"
        if not t.get("bidnet_last_failure_reason"):
            t = dict(t)
            t["bidnet_last_failure_reason"] = "BIDNET_USERNAME/BIDNET_PASSWORD not configured"
    return {
        "kind": "BidNetConnectionStatus",
        "platform": "BidNet",
        "status": status or ("CONNECTED" if t.get("storage_state_present") else "EXPIRED"),
        "bidnet_auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "storage_state_present": t.get("storage_state_present"),
        "last_successful_login": t.get("bidnet_last_auth_success"),
        "last_session_reuse": t.get("bidnet_last_session_reuse"),
        "last_authenticated_run": t.get("bidnet_last_authenticated_run"),
        "last_failure_reason": t.get("bidnet_last_failure_reason"),
        "details_recovered": t.get("bidnet_authenticated_detail_recovered") or 0,
        "documents_recovered": t.get("bidnet_documents_recovered") or 0,
        "economics_ready": t.get("bidnet_economics_ready") or 0,
        "reported_open": t.get("bidnet_reported_open"),
        "harvested": t.get("bidnet_last_harvested") or 0,
        "pagination_pct": t.get("bidnet_pagination_pct"),
        "pagination_complete": t.get("bidnet_pagination_complete"),
        "discovery_truncated": t.get("bidnet_discovery_truncated"),
        "existing_enriched": t.get("bidnet_existing_enriched") or 0,
        "net_new_unique": t.get("bidnet_net_new_unique") or 0,
        "detail_failure_counts": t.get("bidnet_detail_failure_counts") or {},
        "last_harvest_at": t.get("bidnet_last_harvest_at"),
        "telemetry": {
            "login_attempts": t.get("bidnet_login_attempts") or 0,
            "login_successes": t.get("bidnet_login_successes") or 0,
            "login_failures": t.get("bidnet_login_failures") or 0,
            "session_reuses": t.get("bidnet_session_reuses") or 0,
            "auth_challenges": t.get("bidnet_auth_challenges") or 0,
        },
    }


def startup_health_report() -> dict[str, Any]:
    """Non-blocking startup report — does not attempt login."""
    status = owner_connection_status()
    msg = (
        f"govtracker: BidNet auth status={status.get('status')} "
        f"enabled={status.get('bidnet_auth_enabled')} "
        f"credentials={status.get('credentials_configured')} "
        f"session={status.get('storage_state_present')}"
    )
    print(msg, flush=True)
    log.info(
        "BidNet auth startup status=%s credentials=%s session=%s",
        status.get("status"),
        status.get("credentials_configured"),
        status.get("storage_state_present"),
    )
    return status


def _scrub(text: str) -> str:
    """Strip anything that looks like a password/username from failure text."""
    cfg = load_bidnet_auth_config()
    out = str(text or "")
    for secret in (cfg.password, cfg.username):
        if secret and len(secret) >= 3 and secret in out:
            out = out.replace(secret, "[REDACTED]")
    return out[:500]
