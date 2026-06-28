"""Simple email + password login for GovTracker."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

COOKIE_NAME = "govtracker_auth"
MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days

PUBLIC_PATHS = {
    "/login.html",
    "/style.css",
    "/api/login",
    "/api/health",
}


def auth_enabled() -> bool:
    return bool(os.getenv("APP_EMAIL", "").strip() and os.getenv("APP_PASSWORD", "").strip())


def _secret() -> str:
    secret = os.getenv("SECRET_KEY", "").strip()
    if secret:
        return secret
    return os.getenv("APP_PASSWORD", "").strip()


def verify_login(email: str, password: str) -> bool:
    expected_email = os.getenv("APP_EMAIL", "").strip().lower()
    expected_password = os.getenv("APP_PASSWORD", "").strip()
    if not expected_email or not expected_password:
        return False
    email_ok = hmac.compare_digest(email.strip().lower(), expected_email)
    password_ok = hmac.compare_digest(password, expected_password)
    return email_ok and password_ok


def create_auth_token() -> str:
    payload = str(int(time.time()))
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_auth_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.split(".", 1)
    expected = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        age = int(time.time()) - int(payload)
    except ValueError:
        return False
    return age <= MAX_AGE_SECONDS


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/login"):
        return True
    return False
