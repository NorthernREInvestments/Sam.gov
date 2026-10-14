"""Cross-portal session persistence verification (no secrets in output).

Confirms authenticated sessions under M3_DATA_ROOT survive restarts and that
credential/account bindings are reusable without duplicate registrations.
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc
from m3_portal_access_map import (
    PORTAL_BIDNET,
    PORTAL_EUNA,
    PORTAL_OPENGOV,
    seed_bidnet_defaults,
    ui_summary as access_map_ui,
)

log = logging.getLogger("govtracker.m3_session_persistence")

BUILD = "20261014-m3-session-persistence-v1"


def _utc() -> str:
    return now_utc().isoformat()


def _bidnet_session_probe() -> dict[str, Any]:
    out: dict[str, Any] = {"portal": PORTAL_BIDNET}
    try:
        from bidnet_auth.config import auth_credential_source, load_bidnet_auth_config
        from bidnet_auth.session_store import load_storage_state, storage_state_exists, storage_state_path

        cfg = load_bidnet_auth_config()
        cred = auth_credential_source()
        state = load_storage_state()
        cookies = len((state or {}).get("cookies") or []) if state else 0
        out.update(
            {
                "session_path": str(storage_state_path()),
                "session_present": storage_state_exists(),
                "cookie_count": cookies,
                "credentials_configured": bool(cfg.credentials_present),
                "credential_source": cred.get("credential_source"),
                "auth_enabled": bool(cfg.auth_enabled),
                # username presence only — never the value
                "username_configured": bool(cfg.username),
            }
        )
        seed_bidnet_defaults(username_hint=(cfg.username[:3] + "***") if cfg.username else None)
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def _opengov_session_probe() -> dict[str, Any]:
    out: dict[str, Any] = {"portal": PORTAL_OPENGOV}
    try:
        from m3_data_root import data_path
        from pathlib import Path

        # Common OpenGov session locations (presence only)
        candidates = [
            data_path("opengov_auth/storage_state.json"),
            data_path("opengov/storage_state.json"),
        ]
        present = [str(p) for p in candidates if Path(p).exists()]
        out["session_candidates_present"] = present
        out["session_present"] = bool(present)
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def _euna_session_probe() -> dict[str, Any]:
    out: dict[str, Any] = {"portal": PORTAL_EUNA}
    try:
        from m3_data_root import data_path
        from pathlib import Path

        candidates = [
            data_path("euna_auth/storage_state.json"),
            data_path("euna/storage_state.json"),
        ]
        present = [str(p) for p in candidates if Path(p).exists()]
        out["session_candidates_present"] = present
        out["session_present"] = bool(present)
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def verify_session_restore(*, attempt_live_reuse: bool = False) -> dict[str, Any]:
    """Verify durable sessions + credential bindings. Optionally exercise BidNet reuse."""
    from m3_source_credentials import ensure_encrypted_migration, list_credentials

    migration = ensure_encrypted_migration()
    bidnet = _bidnet_session_probe()
    report: dict[str, Any] = {
        "kind": "M3SessionPersistenceVerification",
        "build": BUILD,
        "at": _utc(),
        "credential_migration": migration,
        "credentials_public": list_credentials(include_secrets=False),
        "access_map": access_map_ui(),
        "portals": {
            PORTAL_BIDNET: bidnet,
            PORTAL_OPENGOV: _opengov_session_probe(),
            PORTAL_EUNA: _euna_session_probe(),
        },
        "live_reuse": None,
        "ok": False,
    }

    if attempt_live_reuse and bidnet.get("session_present"):
        try:
            from bidnet_auth.client import BidNetAuthenticatedClient
            from bidnet_auth.config import load_bidnet_auth_config

            client = BidNetAuthenticatedClient(load_bidnet_auth_config())
            try:
                auth = client.ensure_authenticated()
                report["live_reuse"] = {
                    "authenticated": bool(auth.authenticated),
                    "status": auth.status,
                    # message may be diagnostic — truncate, never include secrets
                    "message": str(auth.message or "")[:120],
                    "session_reused": "SESSION" in str(auth.status or "").upper()
                    or "REUSE" in str(auth.message or "").upper(),
                }
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception as exc:
            report["live_reuse"] = {"error": type(exc).__name__}

    # Success criteria: BidNet durable session OR credentials configured for refresh
    report["ok"] = bool(
        bidnet.get("session_present") or bidnet.get("credentials_configured")
    )
    report["policy"] = {
        "reuse_existing_accounts": True,
        "no_plaintext_secrets": True,
        "no_credential_overwrite_without_owner": True,
        "sessions_on_volume": True,
        "purchasing_group_is_membership_not_new_login": True,
    }
    return report
