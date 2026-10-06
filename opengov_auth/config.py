"""OpenGov auth environment configuration. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_LOGIN_URL = "https://procurement.opengov.com/login"
DEFAULT_VERIFY_URL = "https://procurement.opengov.com/"
DEFAULT_NETWORK_URL = "https://procurement.opengov.com/"
DEFAULT_STORAGE_REL = "opengov_auth/storage_state.json"

# Owner vendor NAICS prefs must NOT define M3 discovery scope
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
class OpenGovAuthConfig:
    auth_enabled: bool
    username: str
    password: str
    login_url: str
    verify_url: str
    network_url: str
    storage_state_path: str | None
    headless: bool
    discovery_batch_size: int
    recovery_batch_size: int
    backlog_batch_size: int
    discovery_scope: str

    @property
    def credentials_present(self) -> bool:
        return bool(self.username and self.password)

    @property
    def can_authenticate(self) -> bool:
        return self.auth_enabled and self.credentials_present


def load_opengov_auth_config() -> OpenGovAuthConfig:
    return OpenGovAuthConfig(
        auth_enabled=_truthy(os.environ.get("OPENGOV_AUTH_ENABLED"), True),
        username=(os.environ.get("OPENGOV_USERNAME") or "").strip(),
        password=os.environ.get("OPENGOV_PASSWORD") or "",
        login_url=(os.environ.get("OPENGOV_LOGIN_URL") or DEFAULT_LOGIN_URL).strip(),
        verify_url=(os.environ.get("OPENGOV_VERIFY_URL") or DEFAULT_VERIFY_URL).strip(),
        network_url=(os.environ.get("OPENGOV_NETWORK_URL") or DEFAULT_NETWORK_URL).strip(),
        storage_state_path=(os.environ.get("OPENGOV_STORAGE_STATE_PATH") or "").strip() or None,
        headless=_truthy(os.environ.get("OPENGOV_HEADLESS"), True),
        discovery_batch_size=_int_env("OPENGOV_DISCOVERY_BATCH_SIZE", 40),
        recovery_batch_size=_int_env("OPENGOV_RECOVERY_BATCH_SIZE", 100),
        backlog_batch_size=_int_env("OPENGOV_BACKLOG_BATCH_SIZE", 100),
        discovery_scope=M3_DISCOVERY_SCOPE,
    )
