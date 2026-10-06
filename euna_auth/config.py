"""Euna/Bonfire auth environment configuration. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass

from euna_auth.states import OPTIONAL_TARGETED_SOURCE

# Start on Supplier Network so account.bonfirehub.com receives a valid flow= UUID.
# Post-login verify must NOT use /opportunities — that route is Cloudflare-gated in headless.
DEFAULT_LOGIN_URL = "https://vendor.bonfirehub.com/login?bounceUrl=%2Fagencies"
DEFAULT_VERIFY_URL = "https://vendor.bonfirehub.com/agencies"
DEFAULT_OPPORTUNITIES_URL = "https://vendor.bonfirehub.com/opportunities"
DEFAULT_STORAGE_REL = "euna_auth/storage_state.json"
M3_DISCOVERY_SCOPE = "BROAD_PRODUCT_RESALE"
ACCOUNT_CATEGORY_RESTRICTION = "ACCOUNT_CATEGORY_RESTRICTION"
SOURCE_ROLE = OPTIONAL_TARGETED_SOURCE


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _normalize_secret(raw: str | None) -> str:
    """Strip whitespace and accidental surrounding quotes from env secrets."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        s = s[1:-1].strip()
    return s


def _parse_enabled_states(raw: str | None) -> tuple[str, ...]:
    """Parse EUNA_ENABLED_STATES=NE,WY,CO → frozenset of uppercase state codes."""
    if not raw or not str(raw).strip():
        return ()
    parts = []
    for tok in re_split_states(str(raw)):
        code = tok.strip().upper()
        if len(code) == 2 and code.isalpha():
            parts.append(code)
    # preserve order, unique
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def re_split_states(raw: str) -> list[str]:
    import re

    return [x for x in re.split(r"[,;\s]+", raw) if x.strip()]


@dataclass(frozen=True)
class EunaAuthConfig:
    auth_enabled: bool
    username: str
    password: str
    login_url: str
    verify_url: str
    opportunities_url: str
    storage_state_path: str | None
    headless: bool
    discovery_batch_size: int
    recovery_batch_size: int
    backlog_batch_size: int
    discovery_scope: str
    # Nationwide discovery is OFF by default — Euna is paid state-by-state.
    national_discovery_enabled: bool
    enabled_states: tuple[str, ...]
    source_role: str

    @property
    def credentials_present(self) -> bool:
        return bool(self.username and self.password)

    @property
    def can_authenticate(self) -> bool:
        return self.auth_enabled and self.credentials_present

    @property
    def should_run_scheduled_discovery(self) -> bool:
        """National runs off by default; targeted states only when explicitly listed."""
        if self.national_discovery_enabled:
            return True
        return bool(self.enabled_states)

    @property
    def coverage_category(self) -> str:
        return "PAID_OPTIONAL"

    def credential_hygiene(self) -> dict:
        """Sanitized credential presence report — never includes values."""
        return {
            "username_present": bool(self.username),
            "password_present": bool(self.password),
            "username_length": len(self.username),
            "password_length": len(self.password),
            "username_looks_like_email": ("@" in self.username and "." in self.username),
            "auth_enabled": self.auth_enabled,
            "national_discovery_enabled": self.national_discovery_enabled,
            "enabled_states": list(self.enabled_states),
            "source_role": self.source_role,
            "login_url_host": self.login_url.split("/")[2] if "://" in self.login_url else None,
            "verify_url_host": self.verify_url.split("/")[2] if "://" in self.verify_url else None,
        }


def load_euna_auth_config() -> EunaAuthConfig:
    return EunaAuthConfig(
        auth_enabled=_truthy(os.environ.get("EUNA_AUTH_ENABLED"), True),
        username=_normalize_secret(os.environ.get("EUNA_USERNAME")),
        password=_normalize_secret(os.environ.get("EUNA_PASSWORD")),
        login_url=_normalize_secret(os.environ.get("EUNA_LOGIN_URL")) or DEFAULT_LOGIN_URL,
        verify_url=_normalize_secret(os.environ.get("EUNA_VERIFY_URL")) or DEFAULT_VERIFY_URL,
        opportunities_url=(
            _normalize_secret(os.environ.get("EUNA_OPPORTUNITIES_URL")) or DEFAULT_OPPORTUNITIES_URL
        ),
        storage_state_path=_normalize_secret(os.environ.get("EUNA_STORAGE_STATE_PATH")) or None,
        headless=_truthy(os.environ.get("EUNA_HEADLESS"), True),
        discovery_batch_size=_int_env("EUNA_DISCOVERY_BATCH_SIZE", 30),
        recovery_batch_size=_int_env("EUNA_RECOVERY_BATCH_SIZE", 50),
        backlog_batch_size=_int_env("EUNA_BACKLOG_BATCH_SIZE", 50),
        discovery_scope=M3_DISCOVERY_SCOPE,
        # Default FALSE — do not burn scheduled runs on paid nationwide Euna.
        national_discovery_enabled=_truthy(os.environ.get("EUNA_NATIONAL_DISCOVERY_ENABLED"), False),
        enabled_states=_parse_enabled_states(os.environ.get("EUNA_ENABLED_STATES")),
        source_role=SOURCE_ROLE,
    )
