"""Phase L.12 — exact auth-block classification (not collapsed to AUTH_REQUIRED)."""

from __future__ import annotations

from typing import Any

BUILD = "20260928-m3-phase-l12-auth-walled-history-recovery"

# Platform-level block ≠ history unavailable
PLATFORM_HISTORY_BLOCKED = "PLATFORM_HISTORY_BLOCKED"
HISTORY_NOT_AVAILABLE = "HISTORY_NOT_AVAILABLE"

# Access modes (L.12 exact taxonomy)
PUBLIC_ANTI_BOT_BLOCKED = "PUBLIC_ANTI_BOT_BLOCKED"
FREE_REGISTRATION_REQUIRED = "FREE_REGISTRATION_REQUIRED"
VENDOR_ACCOUNT_REQUIRED = "VENDOR_ACCOUNT_REQUIRED"
BUYER_SPECIFIC_ACCOUNT_REQUIRED = "BUYER_SPECIFIC_ACCOUNT_REQUIRED"
CAPTCHA_PRESENT = "CAPTCHA_PRESENT"
PRIVATE_RESTRICTED = "PRIVATE_RESTRICTED"
NO_PUBLIC_HISTORY_FEATURE = "NO_PUBLIC_HISTORY_FEATURE"
UNKNOWN_ACCESS_MODE = "UNKNOWN_ACCESS_MODE"

# Legacy aliases (L.11) — map into L.12 taxonomy
LEGACY_MAP = {
    "PUBLIC_REGISTRATION_REQUIRED": FREE_REGISTRATION_REQUIRED,
    "AUTH_ACCOUNT_REQUIRED": VENDOR_ACCOUNT_REQUIRED,
    "BUYER_VENDOR_ACCOUNT_REQUIRED": VENDOR_ACCOUNT_REQUIRED,
    "PRIVATE_RESTRICTED": PRIVATE_RESTRICTED,
    "UNKNOWN_AUTH": UNKNOWN_ACCESS_MODE,
    "AUTH_REQUIRED": UNKNOWN_ACCESS_MODE,
    "HISTORY_AUTH_REQUIRED": UNKNOWN_ACCESS_MODE,
}

ACCESS_MODES = (
    PUBLIC_ANTI_BOT_BLOCKED,
    FREE_REGISTRATION_REQUIRED,
    VENDOR_ACCOUNT_REQUIRED,
    BUYER_SPECIFIC_ACCOUNT_REQUIRED,
    CAPTCHA_PRESENT,
    PRIVATE_RESTRICTED,
    NO_PUBLIC_HISTORY_FEATURE,
    UNKNOWN_ACCESS_MODE,
)


def normalize_access_mode(mode: str | None) -> str:
    if not mode:
        return UNKNOWN_ACCESS_MODE
    s = str(mode).strip().upper()
    if s in ACCESS_MODES:
        return s
    return LEGACY_MAP.get(s, UNKNOWN_ACCESS_MODE)


def classify_access_mode(
    blob: str | None = None,
    *,
    status: str | None = None,
    http_status: int | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Classify blocked history access — never collapse to a single AUTH_REQUIRED."""
    t = f"{blob or ''} {status or ''} {platform or ''}".lower()
    code = http_status

    if code == 202 or "just a moment" in t or "cloudflare" in t or "challenge-platform" in t:
        return {
            "access_mode": PUBLIC_ANTI_BOT_BLOCKED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_ANTI_BOT",
        }
    if any(x in t for x in ("captcha", "recaptcha", "hcaptcha", "cf-browser-verification")):
        return {
            "access_mode": CAPTCHA_PRESENT,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_CAPTCHA",
        }
    if any(x in t for x in ("buyer portal", "agency login", "entity registration", "buyer-specific")):
        return {
            "access_mode": BUYER_SPECIFIC_ACCOUNT_REQUIRED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_BUYER_ACCOUNT",
        }
    if any(
        x in t
        for x in (
            "vendor login",
            "supplier portal",
            "vendor account",
            "supplier registration",
            "supplier account",
            "bidnet",
        )
    ):
        # BidNet / supplier networks: free vendor reg often unlocks packages
        if any(x in t for x in ("free registration", "register for free", "create a free")):
            return {
                "access_mode": FREE_REGISTRATION_REQUIRED,
                "platform_history_blocked": True,
                "history_not_available": False,
                "rule": "L12_FREE_REG",
            }
        return {
            "access_mode": VENDOR_ACCOUNT_REQUIRED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_VENDOR_ACCOUNT",
        }
    if any(x in t for x in ("register", "sign up", "create account", "create a free", "free account", "free registration")):
        return {
            "access_mode": FREE_REGISTRATION_REQUIRED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_FREE_REG",
        }
    if any(x in t for x in ("private", "restricted", "authorized users only", "cac required", "not publicly available")):
        return {
            "access_mode": PRIVATE_RESTRICTED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_PRIVATE",
        }
    if any(x in t for x in ("no award history", "results not published", "no public results", "history not available")):
        return {
            "access_mode": NO_PUBLIC_HISTORY_FEATURE,
            "platform_history_blocked": False,
            "history_not_available": True,
            "rule": "L12_NO_PUBLIC_FEATURE",
        }
    if code in {401, 403} or any(x in t for x in ("login", "sign in", "authenticate", "forbidden", "auth")):
        return {
            "access_mode": UNKNOWN_ACCESS_MODE,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_UNKNOWN_AUTH",
        }
    # BidNet default when platform known blocked without detail
    if platform and "bidnet" in str(platform).lower():
        return {
            "access_mode": PUBLIC_ANTI_BOT_BLOCKED,
            "platform_history_blocked": True,
            "history_not_available": False,
            "rule": "L12_BIDNET_DEFAULT_ANTIBOT",
        }
    return {
        "access_mode": UNKNOWN_ACCESS_MODE,
        "platform_history_blocked": True,
        "history_not_available": False,
        "rule": "L12_DEFAULT_UNKNOWN",
    }


# Back-compat shim for L.11 callers
def classify_auth_wall(blob: str | None, *, status: str | None = None) -> str:
    return classify_access_mode(blob, status=status)["access_mode"]
