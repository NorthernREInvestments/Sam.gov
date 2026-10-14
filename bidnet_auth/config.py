"""BidNet auth environment configuration. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


DEFAULT_LOGIN_URL = "https://www.bidnetdirect.com/public/authentication/login"
DEFAULT_VERIFY_URL = "https://www.bidnetdirect.com/private/supplier/solicitations/search"
DEFAULT_SEARCH_URL = "https://www.bidnetdirect.com/private/supplier/solicitations/search"
DEFAULT_STORAGE_REL = "bidnet_auth/storage_state.json"

# Owner vendor NAICS/profile codes must NOT define M3 discovery scope
M3_DISCOVERY_SCOPE = "BROAD_PRODUCT_RESALE"
VENDOR_PROFILE_CODES = "VENDOR_PROFILE_CODES"
ACCOUNT_CATEGORY_RESTRICTION = "ACCOUNT_CATEGORY_RESTRICTION"


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class BidNetAuthConfig:
    auth_enabled: bool
    username: str
    password: str
    login_url: str
    verify_url: str
    search_url: str
    storage_state_path: str | None
    headless: bool
    recovery_batch_size: int
    backlog_batch_size: int
    harvest_batch_size: int
    discovery_scope: str

    @property
    def credentials_present(self) -> bool:
        return bool(self.username and self.password)

    @property
    def can_authenticate(self) -> bool:
        return self.auth_enabled and self.credentials_present


def _env_first(*names: str) -> str:
    """Backward-compatible env lookup — first non-empty wins. Never logs values."""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw)
    return ""


def _portal_store_credentials() -> tuple[str, str, str | None]:
    """Fall back to Settings → Credentials portal store (bidnet / bidnetdirect).

    Internal wiring repair: auth previously only read env vars while the UI
    persisted credentials to AppSetting m3_portal_credentials_v1.
    """
    try:
        from m3_source_credentials import get_credential

        for key in ("bidnet", "bidnetdirect", "bidnet_direct", "bid_net"):
            rec = get_credential(key, include_secrets=True)
            if not isinstance(rec, dict):
                continue
            user = str(rec.get("username") or "").strip()
            password = str(rec.get("password") or "")
            login_url = str(rec.get("login_url") or "").strip() or None
            if user and password:
                return user, password, login_url
    except Exception:
        pass
    return "", "", None


def load_bidnet_auth_config() -> BidNetAuthConfig:
    username = _env_first(
        "BIDNET_USERNAME",
        "BIDNET_USER",
        "BIDNET_EMAIL",
        "BIDNET_LOGIN",
        "BIDNETDIRECT_USERNAME",
    ).strip()
    password = _env_first(
        "BIDNET_PASSWORD",
        "BIDNET_PASS",
        "BIDNETDIRECT_PASSWORD",
    )
    login_url = _env_first("BIDNET_LOGIN_URL", "BIDNETDIRECT_LOGIN_URL").strip() or DEFAULT_LOGIN_URL

    if not (username and password):
        portal_user, portal_pass, portal_login = _portal_store_credentials()
        if portal_user and portal_pass:
            username = portal_user
            password = portal_pass
            if portal_login:
                login_url = portal_login

    return BidNetAuthConfig(
        auth_enabled=_truthy(_env_first("BIDNET_AUTH_ENABLED", "BIDNETDIRECT_AUTH_ENABLED") or None, True),
        username=username,
        password=password,
        login_url=login_url,
        verify_url=(_env_first("BIDNET_VERIFY_URL") or DEFAULT_VERIFY_URL).strip(),
        search_url=(_env_first("BIDNET_SEARCH_URL") or DEFAULT_SEARCH_URL).strip(),
        storage_state_path=_env_first("BIDNET_STORAGE_STATE_PATH", "BIDNET_SESSION_PATH").strip() or None,
        headless=_truthy(os.environ.get("BIDNET_HEADLESS"), True),
        recovery_batch_size=_int_env("BIDNET_RECOVERY_BATCH_SIZE", 200),
        backlog_batch_size=_int_env("BIDNET_BACKLOG_BATCH_SIZE", 250),
        harvest_batch_size=_int_env("BIDNET_HARVEST_BATCH_SIZE", 100),
        discovery_scope=M3_DISCOVERY_SCOPE,
    )


def auth_credential_source() -> dict[str, Any]:
    """Diagnostics only — never includes secret values."""
    env_user = bool(
        _env_first(
            "BIDNET_USERNAME",
            "BIDNET_USER",
            "BIDNET_EMAIL",
            "BIDNET_LOGIN",
            "BIDNETDIRECT_USERNAME",
        ).strip()
    )
    env_pass = bool(
        _env_first("BIDNET_PASSWORD", "BIDNET_PASS", "BIDNETDIRECT_PASSWORD")
    )
    portal_user, portal_pass, _ = _portal_store_credentials()
    cfg = load_bidnet_auth_config()
    source = "none"
    if env_user and env_pass:
        source = "environment"
    elif portal_user and portal_pass:
        source = "portal_credential_store"
    return {
        "auth_enabled": cfg.auth_enabled,
        "credentials_configured": cfg.credentials_present,
        "credential_source": source,
        "env_username_present": env_user,
        "env_password_present": env_pass,
        "portal_store_credentials_present": bool(portal_user and portal_pass),
        "legacy_env_aliases_supported": True,
        "storage_state_path_override": bool(cfg.storage_state_path),
    }
