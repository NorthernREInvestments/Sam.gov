"""Persistent portal credential store with encrypted secrets.

Rules (permanent across all procurement sources):
- Reuse existing portal accounts whenever possible.
- Never store passwords / tokens in plaintext files, logs, source, or business records.
- Encrypt secrets at rest (Fernet); key from M3_CREDENTIALS_FERNET_KEY or SECRET_KEY.
- Do not overwrite username/password/recovery fields without owner_approved_overwrite.
- Prefer Railway/env secrets for primary accounts; this store is the durable UI/runtime fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import string
from copy import deepcopy
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_credentials")

SETTINGS_KEY = "m3_portal_credentials_v1"
ENC_PREFIX = "enc:v1:"
BUILD = "20261014-m3-portal-credentials-encrypted-v1"


def _utc() -> str:
    return now_utc().isoformat()


def _fernet():
    from cryptography.fernet import Fernet

    raw = (os.environ.get("M3_CREDENTIALS_FERNET_KEY") or "").strip()
    if raw:
        key = raw.encode() if not raw.startswith("gAAAA") else raw.encode()
        # Accept either raw Fernet key or any passphrase → derive
        try:
            return Fernet(key)
        except Exception:
            dig = hashlib.sha256(raw.encode()).digest()
            return Fernet(base64.urlsafe_b64encode(dig))
    secret = (os.environ.get("SECRET_KEY") or os.environ.get("APP_PASSWORD") or "").strip()
    if not secret:
        # Deterministic local-only fallback so unit tests work; production must set SECRET_KEY
        secret = "m3-dev-only-credential-key-not-for-production"
        log.warning("M3 credentials encryption using ephemeral fallback key — set SECRET_KEY")
    dig = hashlib.sha256(f"m3-portal-creds-v1:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(dig))


def encrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if str(value).startswith(ENC_PREFIX):
        return str(value)
    token = _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")
    return f"{ENC_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    raw = str(value)
    if not raw.startswith(ENC_PREFIX):
        # Legacy plaintext — return as-is; caller should re-save encrypted
        return raw
    token = raw[len(ENC_PREFIX) :].encode("ascii")
    return _fernet().decrypt(token).decode("utf-8")


def generate_portal_password(*, length: int = 24) -> str:
    """Cryptographically strong password for portals that require a new login.

    Never log the returned value. Persist only via upsert_credential (encrypted).
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    # Ensure complexity
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(max(16, length)))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*-_=+" for c in pwd)
        ):
            return pwd


def _load() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    return data
        finally:
            db.close()
    except Exception:
        log.debug("credential load failed", exc_info=True)
    return {"kind": "M3PortalCredentials", "build": BUILD, "portals": {}, "updated_at": None}


def _save(state: dict[str, Any]) -> None:
    state = deepcopy(state)
    state["kind"] = "M3PortalCredentials"
    state["build"] = BUILD
    state["updated_at"] = _utc()
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            payload = json.dumps(state, default=str)
            if row:
                row.value = payload
            else:
                db.add(AppSetting(key=SETTINGS_KEY, value=payload))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("credential save failed")


def _public_rec(rec: dict[str, Any]) -> dict[str, Any]:
    r = dict(rec)
    has_pw = bool(r.pop("password", None))
    r.pop("password_recovery_hint", None)
    r["password_set"] = has_pw
    r["password"] = "********" if has_pw else ""
    # Never expose encrypted blob in list API either
    return r


def list_credentials(*, include_secrets: bool = False) -> dict[str, Any]:
    state = _load()
    portals = {}
    for key, rec in (state.get("portals") or {}).items():
        if include_secrets:
            r = dict(rec)
            try:
                r["password"] = decrypt_secret(r.get("password"))
            except Exception:
                r["password"] = None
                r["decrypt_error"] = True
            portals[key] = r
        else:
            portals[key] = _public_rec(dict(rec))
    return {
        "kind": "M3PortalCredentials",
        "build": BUILD,
        "portals": portals,
        "updated_at": state.get("updated_at"),
        "secrets_encrypted_at_rest": True,
    }


def upsert_credential(portal: str, body: dict[str, Any]) -> dict[str, Any]:
    portal_key = str(portal or body.get("portal") or "").strip().lower().replace(" ", "_")
    if not portal_key:
        return {"ok": False, "error": "portal required"}
    state = _load()
    portals = dict(state.get("portals") or {})
    existing = dict(portals.get(portal_key) or {})
    owner_approved = bool(body.get("owner_approved_overwrite"))

    # Protect existing credentials from silent overwrite
    new_user = body.get("username")
    new_pass = body.get("password")
    if new_pass == "********":
        new_pass = None  # UI mask — keep existing
    if existing.get("username") or existing.get("password"):
        changing_user = new_user is not None and str(new_user) != str(existing.get("username") or "")
        changing_pass = new_pass is not None and new_pass != ""
        changing_recovery = (
            body.get("password_recovery_hint") is not None
            and body.get("password_recovery_hint") != existing.get("password_recovery_hint")
        )
        if (changing_user or changing_pass or changing_recovery) and not owner_approved:
            return {
                "ok": False,
                "error": "owner_approved_overwrite_required",
                "message": (
                    "Existing portal credentials/recovery info are protected. "
                    "Pass owner_approved_overwrite=true after explicit owner approval."
                ),
                "portal": portal_key,
            }

    password_plain = new_pass if new_pass is not None else None
    if password_plain is None:
        password_stored = existing.get("password")
    else:
        password_stored = encrypt_secret(password_plain)

    # Migrate legacy plaintext on touch
    if password_stored and not str(password_stored).startswith(ENC_PREFIX):
        try:
            password_stored = encrypt_secret(decrypt_secret(password_stored))
        except Exception:
            password_stored = encrypt_secret(str(password_stored))

    rec = {
        "portal": portal_key,
        "source_id": body.get("source_id") or existing.get("source_id") or portal_key,
        "login_url": body.get("login_url") if body.get("login_url") is not None else existing.get("login_url"),
        "username": new_user if new_user is not None else existing.get("username"),
        "password": password_stored,
        "account_notes": (
            body.get("account_notes")
            if body.get("account_notes") is not None
            else existing.get("account_notes")
        ),
        "account_status": body.get("account_status") or existing.get("account_status") or "CONFIGURED",
        "last_successful_login": existing.get("last_successful_login"),
        "last_failure": existing.get("last_failure"),
        "mfa_or_manual_login_required": bool(
            body.get("mfa_or_manual_login_required")
            if body.get("mfa_or_manual_login_required") is not None
            else existing.get("mfa_or_manual_login_required")
        ),
        "password_recovery_hint": (
            body.get("password_recovery_hint")
            if body.get("password_recovery_hint") is not None
            else existing.get("password_recovery_hint")
        ),
        "created_by": existing.get("created_by") or body.get("created_by") or "owner_or_system",
        "updated_at": _utc(),
    }
    if body.get("generate_password") and not existing.get("password"):
        # Only auto-generate when no password exists yet (new account)
        generated = generate_portal_password()
        rec["password"] = encrypt_secret(generated)
        rec["password_generated_at"] = _utc()
        # Return once to caller for owner vaulting — never log
        portals[portal_key] = rec
        state["portals"] = portals
        _save(state)
        public = _public_rec(dict(rec))
        return {
            "ok": True,
            "credential": public,
            "generated_password_once": generated,
            "note": "Store generated_password_once in owner vault; it will not be shown again",
        }

    portals[portal_key] = rec
    state["portals"] = portals
    _save(state)
    return {"ok": True, "credential": list_credentials()["portals"].get(portal_key)}


def get_credential(portal: str, *, include_secrets: bool = False) -> dict[str, Any] | None:
    key = str(portal or "").strip().lower().replace(" ", "_")
    state = _load()
    rec = (state.get("portals") or {}).get(key)
    if not rec:
        return None
    if include_secrets:
        out = dict(rec)
        try:
            out["password"] = decrypt_secret(out.get("password"))
        except Exception:
            out["password"] = None
            out["decrypt_error"] = True
        return out
    return _public_rec(dict(rec))


def mark_login_result(portal: str, *, success: bool, reason: str | None = None) -> None:
    state = _load()
    key = str(portal or "").strip().lower().replace(" ", "_")
    rec = dict((state.get("portals") or {}).get(key) or {"portal": key})
    if success:
        rec["last_successful_login"] = _utc()
        rec["account_status"] = "AUTHENTICATED"
        rec["last_failure"] = None
    else:
        # Never put secret material into failure reason
        safe = str(reason or "auth_failed")[:160]
        for token in ("password", "passwd", "secret", "token"):
            if token in safe.lower():
                safe = "auth_failed"
                break
        rec["last_failure"] = {"at": _utc(), "reason": safe}
        rec["account_status"] = "AUTH_FAILED"
    # Encrypt legacy plaintext if present
    if rec.get("password") and not str(rec["password"]).startswith(ENC_PREFIX):
        try:
            rec["password"] = encrypt_secret(decrypt_secret(rec["password"]))
        except Exception:
            pass
    portals = dict(state.get("portals") or {})
    portals[key] = rec
    state["portals"] = portals
    _save(state)


def ensure_encrypted_migration() -> dict[str, Any]:
    """One-shot: encrypt any legacy plaintext passwords in the store."""
    state = _load()
    changed = 0
    portals = dict(state.get("portals") or {})
    for key, rec in list(portals.items()):
        r = dict(rec)
        pw = r.get("password")
        if pw and not str(pw).startswith(ENC_PREFIX):
            r["password"] = encrypt_secret(str(pw))
            portals[key] = r
            changed += 1
    if changed:
        state["portals"] = portals
        _save(state)
    return {"migrated": changed, "build": BUILD}
