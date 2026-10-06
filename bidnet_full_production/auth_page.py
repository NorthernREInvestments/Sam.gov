"""Classify BidNet HTTP/page auth state."""

from __future__ import annotations

import re

from bidnet_full_production.models import (
    ACCESS_DENIED,
    AUTH_UNKNOWN,
    AUTHENTICATED_VALID,
    LOGIN_REQUIRED,
    PACKAGE_LOCKED,
    PUBLIC_PAGE,
    REGISTRATION_REDIRECT,
    SESSION_EXPIRED,
)


def classify_auth_page(*, final_url: str, html: str, session_expected: bool = True) -> str:
    blob = f"{final_url or ''}\n{html or ''}"
    low = blob.lower()
    if re.search(r"public/authentication/login|sign\s*in\s*to\s*continue|please\s*log\s*in", low):
        return LOGIN_REQUIRED if session_expected else PUBLIC_PAGE
    if re.search(r"abstractregisternow|user-registration|vendor registration", low):
        return REGISTRATION_REDIRECT
    if re.search(r"access denied|403 forbidden|not authorized", low):
        return ACCESS_DENIED
    if re.search(r"registered members only|member-only-info|get instant access to solicitation", low):
        return PACKAGE_LOCKED
    if re.search(r"log\s*out|sign\s*out|private/supplier|my\s*account", low):
        return AUTHENTICATED_VALID
    if "/public/" in (final_url or "").lower() and session_expected:
        return PUBLIC_PAGE
    if session_expected and "mets-field" in low:
        return AUTHENTICATED_VALID
    if not session_expected:
        return PUBLIC_PAGE
    return AUTH_UNKNOWN
