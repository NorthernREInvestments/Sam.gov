"""Classify Euna login page outcomes into granular failure reasons. Never logs secrets."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from euna_auth.states import (
    ACCOUNT_ACTIVE,
    ACCOUNT_EMAIL_NOT_VERIFIED,
    ACCOUNT_INACTIVE,
    ACCOUNT_NOT_APPROVED,
    ACCOUNT_PENDING_APPROVAL,
    ACCOUNT_PROFILE_INCOMPLETE,
    AUTH_PROVIDER_ERROR,
    CLOUDFLARE_CHALLENGE,
    SUPPLIER_NETWORK_NOT_ENABLED,
    BAD_CREDENTIALS,
    CAPTCHA,
    EMAIL_VERIFICATION_REQUIRED,
    MFA_REQUIRED,
    NETWORK_TIMEOUT,
    PASSWORD_REJECTED,
    SESSION_CONFLICT,
    SSO_REQUIRED,
    UNKNOWN_AUTH_FAILURE,
    USERNAME_NOT_FOUND,
    WRONG_FORM_FLOW,
    WRONG_LOGIN_HOST,
)


def _text(html: str, visible_text: str | None = None) -> str:
    parts = [html or "", visible_text or ""]
    return "\n".join(parts).lower()


def detect_account_activation(html: str, *, visible_text: str | None = None, url: str = "") -> str:
    blob = _text(html, visible_text)
    if re.search(r"pending\s+approval|awaiting\s+approval|not\s+yet\s+approved", blob):
        return ACCOUNT_PENDING_APPROVAL
    if re.search(r"verify\s+your\s+email|email\s+not\s+verified|confirm\s+your\s+email", blob):
        return ACCOUNT_EMAIL_NOT_VERIFIED
    if re.search(r"complete\s+your\s+profile|profile\s+incomplete|finish\s+setup", blob):
        return ACCOUNT_PROFILE_INCOMPLETE
    if re.search(r"account\s+(?:is\s+)?inactive|deactivated|suspended|disabled", blob):
        return ACCOUNT_INACTIVE
    if re.search(r"not\s+approved|approval\s+required", blob):
        return ACCOUNT_NOT_APPROVED
    # Logged-in vendor without ESN browse entitlement
    if re.search(
        r"sign\s+up\s+for\s+euna\s+supplier\s+network|no\s+agencies\s+available|"
        r"do\s+not\s+have\s+any\s+agencies|upgrade\s+to\s+(?:euna\s+)?supplier|"
        r"supplier\s+network\s+pro",
        blob,
    ):
        return SUPPLIER_NETWORK_NOT_ENABLED
    if "performing security verification" in blob or ("just a moment" in blob and "cloudflare" in blob):
        return CLOUDFLARE_CHALLENGE
    if "vendor.bonfirehub.com" in (url or "") and "login" not in (url or "").lower():
        return ACCOUNT_ACTIVE
    if re.search(r"log\s*out|sign\s*out|opportunities|dashboard", blob):
        return ACCOUNT_ACTIVE
    return ACCOUNT_ACTIVE


def classify_login_failure(
    *,
    html: str,
    url: str,
    visible_text: str | None = None,
    exception_name: str | None = None,
    had_password_field: bool = False,
    had_email_field: bool = False,
    flow_present: bool = False,
) -> dict[str, Any]:
    """Return {reason, account_state, visible_error, flow_detected}."""
    blob = _text(html, visible_text)
    host = urlparse(url or "").netloc.lower()
    visible_error = None
    for pat in (
        r"(invalid\s+(?:email|password|credentials|login)[^.\n]{0,80})",
        r"(incorrect\s+password[^.\n]{0,80})",
        r"(user(?:name)?\s+not\s+found[^.\n]{0,80})",
        r"(we\s+couldn'?t\s+find[^.\n]{0,80})",
        r"(too\s+many\s+attempts[^.\n]{0,80})",
        r"(account\s+(?:is\s+)?(?:locked|suspended|inactive)[^.\n]{0,80})",
        r"(verify\s+your\s+email[^.\n]{0,80})",
        r"(pending\s+approval[^.\n]{0,80})",
        r"(sso\s+required|sign\s+in\s+with\s+(?:microsoft|google|okta)[^.\n]{0,80})",
    ):
        m = re.search(pat, blob, re.I)
        if m:
            visible_error = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
            break

    if exception_name in {"TimeoutError", "Timeout", "ConnectError", "NetworkError"}:
        return {
            "reason": NETWORK_TIMEOUT,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "email_continue_password" if flow_present else "unknown",
            "auth_host": host,
        }

    if re.search(r"captcha|recaptcha|hcaptcha|cf-challenge|just a moment", blob):
        return {
            "reason": CAPTCHA,
            "account_state": None,
            "visible_error": visible_error or "captcha_or_bot_challenge",
            "flow_detected": "challenge",
            "auth_host": host,
        }

    if re.search(r"two[- ]factor|mfa|authenticator|verification code|one[- ]time", blob):
        return {
            "reason": MFA_REQUIRED,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "mfa",
            "auth_host": host,
        }

    if re.search(
        r"sign\s+in\s+with\s+(microsoft|google|okta|sso)|sso\s+required|single\s+sign[- ]on|login\.microsoftonline",
        blob,
    ) or "login.microsoftonline.com" in (url or "").lower():
        return {
            "reason": SSO_REQUIRED,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "sso",
            "auth_host": host,
        }

    if re.search(r"verify\s+your\s+email|email\s+not\s+verified|confirm\s+your\s+email", blob):
        return {
            "reason": EMAIL_VERIFICATION_REQUIRED,
            "account_state": ACCOUNT_EMAIL_NOT_VERIFIED,
            "visible_error": visible_error,
            "flow_detected": "email_verification",
            "auth_host": host,
        }

    account = detect_account_activation(html, visible_text=visible_text, url=url)
    if account in {
        ACCOUNT_PENDING_APPROVAL,
        ACCOUNT_NOT_APPROVED,
        ACCOUNT_INACTIVE,
        ACCOUNT_PROFILE_INCOMPLETE,
        ACCOUNT_EMAIL_NOT_VERIFIED,
    }:
        return {
            "reason": account,
            "account_state": account,
            "visible_error": visible_error,
            "flow_detected": "account_gate",
            "auth_host": host,
        }

    if re.search(r"user(?:name)?\s+not\s+found|no\s+account\s+found|we\s+couldn'?t\s+find", blob):
        return {
            "reason": USERNAME_NOT_FOUND,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "email_continue_password",
            "auth_host": host,
        }

    if re.search(r"incorrect\s+password|wrong\s+password|password\s+(?:is\s+)?incorrect", blob):
        return {
            "reason": PASSWORD_REJECTED,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "email_continue_password",
            "auth_host": host,
        }

    if re.search(r"invalid\s+(?:email|password|credentials|login)|authentication\s+failed", blob):
        return {
            "reason": BAD_CREDENTIALS,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "email_continue_password",
            "auth_host": host,
        }

    if re.search(r"already\s+signed\s+in|session\s+conflict|another\s+session", blob):
        return {
            "reason": SESSION_CONFLICT,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "session",
            "auth_host": host,
        }

    if host and "bonfirehub.com" not in host and "euna" not in host:
        return {
            "reason": WRONG_LOGIN_HOST,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "redirect",
            "auth_host": host,
        }

    if had_email_field and not had_password_field and not flow_present:
        return {
            "reason": WRONG_FORM_FLOW,
            "account_state": None,
            "visible_error": visible_error or "password_step_not_reached",
            "flow_detected": "email_only",
            "auth_host": host,
        }

    if "login" in (url or "").lower() and had_password_field:
        # Still on login after submit — most often bad credentials when no clearer text
        return {
            "reason": BAD_CREDENTIALS if visible_error or had_password_field else UNKNOWN_AUTH_FAILURE,
            "account_state": None,
            "visible_error": visible_error or "still_on_login_after_submit",
            "flow_detected": "email_continue_password",
            "auth_host": host,
        }

    if re.search(r"something\s+went\s+wrong|server\s+error|auth(?:entication)?\s+provider", blob):
        return {
            "reason": AUTH_PROVIDER_ERROR,
            "account_state": None,
            "visible_error": visible_error,
            "flow_detected": "provider_error",
            "auth_host": host,
        }

    return {
        "reason": UNKNOWN_AUTH_FAILURE,
        "account_state": None,
        "visible_error": visible_error,
        "flow_detected": "email_continue_password" if flow_present else "unknown",
        "auth_host": host,
    }


def is_authenticated_page(*, html: str, url: str, visible_text: str | None = None) -> bool:
    blob = _text(html, visible_text)
    u = (url or "").lower()
    if "login" in u and re.search(r'type=["\']password["\']', html or "", re.I):
        return False
    # Cloudflare interstitial is not an authenticated app surface
    if "performing security verification" in blob or (
        "just a moment" in blob and "cloudflare" in blob
    ):
        return False
    if "vendor.bonfirehub.com" in u and "login" not in u:
        return True
    if re.search(r"log\s*out|sign\s*out", blob) and "login" not in u:
        return True
    if re.search(r"opportunit|dashboard|my\s*account|agencies", blob) and "login" not in u:
        return True
    return False
