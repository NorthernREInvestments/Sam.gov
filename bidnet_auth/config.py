"""BidNet auth environment configuration. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


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


def load_bidnet_auth_config() -> BidNetAuthConfig:
    return BidNetAuthConfig(
        auth_enabled=_truthy(os.environ.get("BIDNET_AUTH_ENABLED"), True),
        username=(os.environ.get("BIDNET_USERNAME") or "").strip(),
        password=os.environ.get("BIDNET_PASSWORD") or "",
        login_url=(os.environ.get("BIDNET_LOGIN_URL") or DEFAULT_LOGIN_URL).strip(),
        verify_url=(
            os.environ.get("BIDNET_VERIFY_URL") or DEFAULT_VERIFY_URL
        ).strip(),
        search_url=(os.environ.get("BIDNET_SEARCH_URL") or DEFAULT_SEARCH_URL).strip(),
        storage_state_path=(os.environ.get("BIDNET_STORAGE_STATE_PATH") or "").strip() or None,
        headless=_truthy(os.environ.get("BIDNET_HEADLESS"), True),
        recovery_batch_size=_int_env("BIDNET_RECOVERY_BATCH_SIZE", 200),
        backlog_batch_size=_int_env("BIDNET_BACKLOG_BATCH_SIZE", 250),
        harvest_batch_size=_int_env("BIDNET_HARVEST_BATCH_SIZE", 100),
        discovery_scope=M3_DISCOVERY_SCOPE,
    )
