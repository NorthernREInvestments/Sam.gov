"""Precise discovery source failure taxonomy — access vs parse, not generic FETCH_FAILED."""

from __future__ import annotations

import re
from typing import Any

# Primary root-cause classifications (exactly one per failed/nonproductive source)
HTTP_403 = "HTTP_403"
HTTP_401 = "HTTP_401"
HTTP_404 = "HTTP_404"
HTTP_429 = "HTTP_429"
HTTP_5XX = "HTTP_5XX"
BOT_CHALLENGE = "BOT_CHALLENGE"
CAPTCHA = "CAPTCHA"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
AUTH_REQUIRED = "AUTH_REQUIRED"
JAVASCRIPT_APP = "JAVASCRIPT_APP"
PORTAL_MIGRATED = "PORTAL_MIGRATED"
STALE_URL = "STALE_URL"
WRONG_ENDPOINT = "WRONG_ENDPOINT"
PARSER_FAILURE = "PARSER_FAILURE"
HTML_CHANGED = "HTML_CHANGED"
API_CHANGED = "API_CHANGED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
VALID_HEALTHY_ZERO = "VALID_HEALTHY_ZERO"
TLS_NETWORK_FAILURE = "TLS_NETWORK_FAILURE"
TIMEOUT = "TIMEOUT"
REDIRECT_FAILURE = "REDIRECT_FAILURE"
CONTENT_TYPE_MISMATCH = "CONTENT_TYPE_MISMATCH"
DOCUMENT_NOT_LISTING = "DOCUMENT_NOT_LISTING"
SOURCE_DUPLICATE = "SOURCE_DUPLICATE"
SOURCE_RETIRED = "SOURCE_RETIRED"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

# Access vs parse split
ACCESS_FAILED = "ACCESS_FAILED"
ACCESS_SUCCEEDED_PARSE_FAILED = "ACCESS_SUCCEEDED_PARSE_FAILED"
ACCESS_SUCCEEDED_HEALTHY_ZERO = "ACCESS_SUCCEEDED_HEALTHY_ZERO"
ACCESS_SUCCEEDED_PRODUCTIVE = "ACCESS_SUCCEEDED_PRODUCTIVE"

# Provenance tiers
TIER_A_AUTHORITATIVE = "TIER_A_AUTHORITATIVE"
TIER_B_OFFICIAL_ALTERNATE = "TIER_B_OFFICIAL_ALTERNATE"
TIER_C_PUBLIC_INDEX = "TIER_C_PUBLIC_INDEX"
TIER_D_WEB_DISCOVERY = "TIER_D_WEB_DISCOVERY"

# Backoff hours by class
BACKOFF_HOURS: dict[str, float] = {
    HTTP_429: 6,
    HTTP_5XX: 2,
    BOT_CHALLENGE: 24,
    CAPTCHA: 24,
    REGISTRATION_REQUIRED: 168,  # 7d — no anonymous hammering
    AUTH_REQUIRED: 72,
    HTTP_404: 48,
    STALE_URL: 48,
    PORTAL_MIGRATED: 24,
    PARSER_FAILURE: 12,
    TIMEOUT: 1,
    TLS_NETWORK_FAILURE: 2,
    UNKNOWN_FAILURE: 6,
}


def classify_root_cause(per_source: dict[str, Any], *, body_sample: str | None = None) -> dict[str, Any]:
    """
    Map a live_runner per_source row (+ optional body) to one primary root cause.
    """
    stop = str(per_source.get("source_stop_reason") or per_source.get("explicit_state") or "").upper()
    err = str(per_source.get("error") or "")
    err_l = err.lower()
    validation = per_source.get("validation") if isinstance(per_source.get("validation"), dict) else {}
    fail = str(validation.get("failure_type") or "").upper()
    warnings = " ".join(str(w) for w in (validation.get("warnings") or [])).lower()
    meta = per_source.get("request_meta") if isinstance(per_source.get("request_meta"), dict) else {}
    status = meta.get("http_status") or validation.get("http_status")
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None
    text = (body_sample or "").lower()
    raw = int(per_source.get("raw") or per_source.get("records_fetched") or 0)
    ok = bool(per_source.get("ok"))

    secondary: list[str] = []

    # Productive / healthy zero
    if ok and raw > 0:
        return {
            "primary": "OK",
            "access_outcome": ACCESS_SUCCEEDED_PRODUCTIVE,
            "secondary": [],
            "productive": True,
        }
    if ok and raw == 0 and stop in {"HEALTHY_ZERO", "COMPLETED", "NO_PAGINATION", "EMPTY_PAGE"}:
        return {
            "primary": VALID_HEALTHY_ZERO,
            "access_outcome": ACCESS_SUCCEEDED_HEALTHY_ZERO,
            "secondary": [],
            "productive": False,
            "market_covered": True,  # we saw the listing; market may be empty
        }

    # Explicit HTTP
    if status_i == 403 or stop == "HTTP_403" or fail == "HTTP_403":
        primary = HTTP_403
    elif status_i == 401 or stop == "HTTP_401":
        primary = HTTP_401
    elif status_i == 404 or "404" in err_l or stop in {"HTTP_404", "SOURCE_CHANGED"}:
        primary = HTTP_404
    elif status_i == 429 or "429" in err_l or "rate limit" in err_l:
        primary = HTTP_429
    elif status_i is not None and status_i >= 500:
        primary = HTTP_5XX
    elif stop in {"BOT_PROTECTED", "BOT_CHALLENGE"} or fail in {"BOT_PROTECTED", "BOT_CHALLENGE"}:
        primary = BOT_CHALLENGE
    elif "captcha" in err_l or "captcha" in warnings or fail == "CAPTCHA" or "captcha" in text:
        primary = CAPTCHA
    elif stop == "REGISTRATION_REQUIRED" or fail == "REGISTRATION_REQUIRED" or "registration required" in warnings:
        primary = REGISTRATION_REQUIRED
    elif stop == "AUTH_REQUIRED" or fail == "AUTH_REQUIRED":
        primary = AUTH_REQUIRED
    elif "cloudflare" in text or ("just a moment" in text and "cf-" in text):
        primary = BOT_CHALLENGE
    elif "timed out" in err_l or "timeout" in err_l or stop == "TIMEOUT":
        primary = TIMEOUT
    elif "ssl" in err_l or "tls" in err_l or "certificate" in err_l or "getaddrinfo" in err_l or "dns" in err_l:
        primary = TLS_NETWORK_FAILURE
    elif "redirect" in err_l or stop == "REDIRECT_FAILURE":
        primary = REDIRECT_FAILURE
    elif fail == "PARSER_FAILURE" or stop == "PARSER_FAILURE" or "parser" in warnings:
        primary = PARSER_FAILURE
    elif fail == "VALIDATION_FAILURE" and status_i and 200 <= status_i < 300:
        # Got a page but validation/parser failed
        if re.search(r"\b(app\.js|__NEXT_DATA__|ng-app|react-root|spa)\b", text):
            primary = JAVASCRIPT_APP
        else:
            primary = PARSER_FAILURE
        secondary.append("http_200_but_invalid_listing")
    elif stop in {"NO_FETCHER", "TECHNICAL_FAILURE", "SOURCE_EXCEPTION"}:
        primary = UNKNOWN_FAILURE
        secondary.append(stop)
    elif not ok:
        primary = UNKNOWN_FAILURE
    else:
        primary = VALID_HEALTHY_ZERO

    # Refine AUTH vs REGISTRATION from body
    if primary in {AUTH_REQUIRED, HTTP_403, UNKNOWN_FAILURE} and text:
        if re.search(r"free registration|vendor registration|register to (bid|view|respond)", text):
            primary = REGISTRATION_REQUIRED
        elif re.search(r"must (log|sign) in to (view|access)", text):
            primary = AUTH_REQUIRED

    # Access vs parse
    if primary in {
        PARSER_FAILURE,
        HTML_CHANGED,
        API_CHANGED,
        VALID_HEALTHY_ZERO,
        DOCUMENT_NOT_LISTING,
        JAVASCRIPT_APP,
    }:
        # JS app without public API is still access limitation for our HTTP client
        if primary == JAVASCRIPT_APP:
            access_outcome = ACCESS_FAILED
        elif primary == VALID_HEALTHY_ZERO:
            access_outcome = ACCESS_SUCCEEDED_HEALTHY_ZERO
        else:
            access_outcome = ACCESS_SUCCEEDED_PARSE_FAILED
    elif primary in {
        HTTP_403,
        HTTP_401,
        HTTP_404,
        HTTP_429,
        HTTP_5XX,
        BOT_CHALLENGE,
        CAPTCHA,
        REGISTRATION_REQUIRED,
        AUTH_REQUIRED,
        TLS_NETWORK_FAILURE,
        TIMEOUT,
        REDIRECT_FAILURE,
        STALE_URL,
        WRONG_ENDPOINT,
        CONTENT_TYPE_MISMATCH,
    }:
        access_outcome = ACCESS_FAILED
    else:
        access_outcome = ACCESS_FAILED

    # Fixable by software?
    software_fixable = primary in {
        PARSER_FAILURE,
        HTML_CHANGED,
        API_CHANGED,
        STALE_URL,
        WRONG_ENDPOINT,
        PORTAL_MIGRATED,
        CONTENT_TYPE_MISMATCH,
        DOCUMENT_NOT_LISTING,
    }
    external_block = primary in {
        BOT_CHALLENGE,
        CAPTCHA,
        REGISTRATION_REQUIRED,
        AUTH_REQUIRED,
        HTTP_403,
        HTTP_401,
    }

    return {
        "primary": primary,
        "access_outcome": access_outcome,
        "secondary": secondary,
        "productive": False,
        "software_fixable": software_fixable,
        "external_access_block": external_block,
        "backoff_hours": BACKOFF_HOURS.get(primary, 6),
        "http_status": status_i,
    }


def root_cause_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        p = str((r.get("classification") or {}).get("primary") or r.get("primary") or "UNKNOWN")
        counts[p] = counts.get(p, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
