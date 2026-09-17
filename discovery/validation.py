"""Live listing validation — detect CAPTCHA/login/schema breakage."""

from __future__ import annotations

import re
from typing import Any

from discovery.constants import HEALTH_AUTH_REQUIRED, HEALTH_BLOCKED, HEALTH_BROKEN, HEALTH_DEGRADED, HEALTH_HEALTHY


_BLOCK_SIGNALS = (
    r"captcha",
    r"cf-browser-verification",
    r"cloudflare",
    r"access denied",
    r"unusual traffic",
    r"bot detection",
)

# Soft chrome — alone NEVER implies AUTH_REQUIRED
_LOGIN_CHROME = (
    r"sign\s*in",
    r"log\s*in",
    r"login",
    r"register",
    r"create an account",
    r"supplier login",
    r"register to respond",
    r"vendor registration",
)

# Hard gates — viewing opportunities requires auth
_VIEW_GATED = (
    r"(?:must|please|need to)\s+(?:log|sign)\s*in\s+to\s+(?:view|see|access|browse)",
    r"login\s+required\s+to\s+(?:view|access|see)",
    r"sign\s*in\s+required\s+to\s+(?:view|access)",
    r"authenticate\s+to\s+(?:view|access|continue)",
    r"session\s+expired",
    r"unauthorized",
)


def _has_public_listing_structure(text: str, body: str) -> bool:
    """Portal-aware signals that opportunities are publicly visible."""
    from discovery.sciquest import sciquest_has_public_event_structure

    if sciquest_has_public_event_structure(body):
        return True
    if re.search(r'id=["\']OpenSols["\']', body, re.I):
        return True
    if re.search(r"sourcewell-mn\.gov/solicitations/\d+", body, re.I):
        return True
    if re.search(r"status-badge", body, re.I) and re.search(r"(rfp|rfq|bid|solicitation)", text, re.I):
        return True
    # Generic: multiple solicitation/opportunity links
    if len(re.findall(r"href=[\"'][^\"']*(solicitation|opportunity|bid)[^\"']*[\"']", body, re.I)) >= 3:
        return True
    return False


def _is_password_login_form(text: str, body: str) -> bool:
    """True when a credential form dominates and no public listing structure exists."""
    has_password = bool(re.search(r'type=["\']password["\']', body, re.I)) or ("password" in text and "username" in text)
    has_chrome = any(re.search(p, text, re.I) for p in _LOGIN_CHROME)
    return has_password and has_chrome


def validate_listing_response(
    *,
    status_code: int | None,
    content_type: str | None,
    body: str,
    records_found: int,
    parser_warnings: list[str] | None = None,
    expected_kind: str | None = None,
    structure_recognized: bool | None = None,
) -> dict[str, Any]:
    """
    Zero records does NOT automatically mean healthy parser.
    Login/register chrome alone does NOT imply AUTH_REQUIRED when public listings exist.
    """
    warnings = list(parser_warnings or [])
    body_s = body or ""
    text = body_s[:80000].lower()
    ct = (content_type or "").lower()
    public_structure = _has_public_listing_structure(text, body_s)
    if structure_recognized is None:
        structure_recognized = public_structure

    if status_code in {401, 403}:
        return {
            "valid": False,
            "health_status": HEALTH_AUTH_REQUIRED,
            "failure_type": "AUTH_REQUIRED",
            "warnings": warnings + [f"http_{status_code}"],
            "zero_records_ok": False,
            "structure_recognized": False,
        }
    if status_code and status_code >= 400:
        return {
            "valid": False,
            "health_status": HEALTH_BROKEN,
            "failure_type": f"HTTP_{status_code}",
            "warnings": warnings + [f"http_{status_code}"],
            "zero_records_ok": False,
            "structure_recognized": False,
        }

    for pat in _BLOCK_SIGNALS:
        if re.search(pat, text, re.I):
            if records_found > 0 or public_structure:
                warnings.append("block_signal_but_listings_public")
            else:
                return {
                    "valid": False,
                    "health_status": HEALTH_BLOCKED,
                    "failure_type": "CAPTCHA" if "captcha" in pat else "BLOCKED",
                    "warnings": warnings + ["block_page_detected"],
                    "zero_records_ok": False,
                    "structure_recognized": False,
                }

    # Hard view-gated messages
    for pat in _VIEW_GATED:
        if re.search(pat, text, re.I) and records_found == 0 and not public_structure:
            return {
                "valid": False,
                "health_status": HEALTH_AUTH_REQUIRED,
                "failure_type": "AUTH_REQUIRED",
                "warnings": warnings + ["view_gated_message"],
                "zero_records_ok": False,
                "structure_recognized": False,
            }

    # Password login form replacing listing content
    if _is_password_login_form(text, body_s) and records_found == 0 and not public_structure:
        return {
            "valid": False,
            "health_status": HEALTH_AUTH_REQUIRED,
            "failure_type": "AUTH_REQUIRED",
            "warnings": warnings + ["login_form_without_public_listings"],
            "zero_records_ok": False,
            "structure_recognized": False,
        }

    if any(re.search(p, text, re.I) for p in _LOGIN_CHROME):
        if records_found > 0 or public_structure:
            warnings.append("login_chrome_present_but_listings_public")
        # else: soft chrome alone is NOT AUTH_REQUIRED

    for pat in (
        r"500 internal server error",
        r"502 bad gateway",
        r"503 service",
        r"404 not found",
        r"page not found",
    ):
        if re.search(pat, text, re.I):
            if records_found > 0:
                warnings.append("error_signal_but_listings_parsed")
            else:
                return {
                    "valid": False,
                    "health_status": HEALTH_BROKEN,
                    "failure_type": "ERROR_PAGE",
                    "warnings": warnings + ["error_page_detected"],
                    "zero_records_ok": False,
                    "structure_recognized": False,
                }

    # Schema / layout recognition
    recognizable = bool(structure_recognized)
    if expected_kind == "json" or "json" in ct:
        recognizable = text.strip().startswith("{") or text.strip().startswith("[") or recognizable
        if not recognizable:
            warnings.append("unexpected_json_schema")
    elif expected_kind == "rss" or "xml" in ct or "rss" in ct:
        recognizable = "<rss" in text or "<feed" in text or "<item" in text or recognizable
        if not recognizable:
            warnings.append("unexpected_rss_layout")
    else:
        if not recognizable:
            recognizable = any(
                x in text
                for x in (
                    "<table",
                    "solicitation",
                    "opportunity",
                    "bid",
                    "rfp",
                    "rfq",
                    "ifb",
                    "invitation",
                    "procurement",
                    "status-badge",
                    "opensols",
                )
            )
        if not recognizable and records_found == 0:
            warnings.append("html_layout_unrecognized")

    # SciQuest / portal: event structure present but 0 parses => DEGRADED/PARSER_FAILURE
    if public_structure and records_found == 0:
        return {
            "valid": False,
            "health_status": HEALTH_DEGRADED,
            "failure_type": "PARSER_FAILURE",
            "warnings": warnings + ["public_event_structure_present_but_zero_parsed"],
            "zero_records_ok": False,
            "structure_recognized": True,
            "note": "HTTP/public page exposes event structure but parser emitted zero rows",
        }

    if not recognizable and records_found == 0:
        return {
            "valid": False,
            "health_status": HEALTH_BROKEN,
            "failure_type": "PARSER_SCHEMA_CHANGE",
            "warnings": warnings,
            "zero_records_ok": False,
            "structure_recognized": False,
            "note": "Zero records with unrecognized layout — not treated as healthy empty listing",
        }

    # Legitimate zero-open only when structure recognized AND page says empty
    empty_ok = False
    if records_found == 0 and recognizable:
        if re.search(
            r"(no (?:open )?(?:solicitations|opportunities|events|bids) (?:at this time|found|available)|zero open)",
            text,
            re.I,
        ):
            empty_ok = True
        else:
            # Recognizable but silent empty without empty-message => degraded
            return {
                "valid": False,
                "health_status": HEALTH_DEGRADED,
                "failure_type": "PARSER_FAILURE",
                "warnings": warnings + ["recognizable_layout_zero_records_without_empty_notice"],
                "zero_records_ok": False,
                "structure_recognized": True,
            }

    health = HEALTH_HEALTHY
    if warnings:
        health = HEALTH_DEGRADED
    return {
        "valid": True,
        "health_status": health,
        "failure_type": None,
        "warnings": warnings,
        "zero_records_ok": empty_ok or records_found > 0,
        "records_found": records_found,
        "structure_recognized": recognizable,
    }
