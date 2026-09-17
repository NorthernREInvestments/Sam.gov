"""Application operating mode — development, controlled verification, operational."""

from __future__ import annotations

import os
from typing import Any

from application_clock import now_utc

MODE_DEVELOPMENT_NO_OUTREACH = "DEVELOPMENT_NO_OUTREACH"
MODE_CONTROLLED_REAL_WORLD_VERIFICATION = "CONTROLLED_REAL_WORLD_VERIFICATION"
MODE_OPERATIONAL = "OPERATIONAL"

_ENV_KEY = "M3_OPERATING_MODE"
_current: str | None = None
_controlled_authorized_by: str | None = None
_controlled_authorized_at: str | None = None
_controlled_acknowledgment: str | None = None

_ALLOWED_MODES = {
    MODE_DEVELOPMENT_NO_OUTREACH,
    MODE_CONTROLLED_REAL_WORLD_VERIFICATION,
    MODE_OPERATIONAL,
}


def get_operating_mode() -> str:
    global _current
    if _current:
        return _current
    raw = (os.environ.get(_ENV_KEY) or MODE_DEVELOPMENT_NO_OUTREACH).strip().upper()
    if raw == MODE_OPERATIONAL or raw == "OPERATIONAL":
        return MODE_OPERATIONAL
    if raw == MODE_CONTROLLED_REAL_WORLD_VERIFICATION:
        # Env can name the mode, but active controlled verification still needs operator auth
        return MODE_CONTROLLED_REAL_WORLD_VERIFICATION
    return MODE_DEVELOPMENT_NO_OUTREACH


def set_operating_mode(mode: str) -> str:
    """Set mode. Leaving controlled clears controlled authorization."""
    global _current, _controlled_authorized_by, _controlled_authorized_at, _controlled_acknowledgment
    m = str(mode or MODE_DEVELOPMENT_NO_OUTREACH).strip().upper()
    if m not in _ALLOWED_MODES:
        m = MODE_DEVELOPMENT_NO_OUTREACH
    if m != MODE_CONTROLLED_REAL_WORLD_VERIFICATION:
        _controlled_authorized_by = None
        _controlled_authorized_at = None
        _controlled_acknowledgment = None
    _current = m
    return _current


def enable_controlled_real_world_verification(
    *,
    operator_id: str,
    acknowledgment: bool,
    acknowledgment_text: str | None = None,
) -> dict[str, Any]:
    """Explicit operator gate into CONTROLLED_REAL_WORLD_VERIFICATION."""
    global _controlled_authorized_by, _controlled_authorized_at, _controlled_acknowledgment
    if not acknowledgment:
        return {
            "enabled": False,
            "error": "acknowledgment_required",
            "message": "Operator must explicitly acknowledge controlled verification limits",
        }
    if not operator_id or not str(operator_id).strip():
        return {"enabled": False, "error": "operator_id_required"}
    set_operating_mode(MODE_CONTROLLED_REAL_WORLD_VERIFICATION)
    _controlled_authorized_by = str(operator_id).strip()
    _controlled_authorized_at = now_utc().isoformat()
    _controlled_acknowledgment = acknowledgment_text or (
        "Operator authorizes controlled real-world verification only: "
        "no automatic outreach, bids, registrations, signatures, or commitments."
    )
    return mode_snapshot()


def disable_controlled_real_world_verification(*, operator_id: str | None = None) -> dict[str, Any]:
    """Return to DEVELOPMENT_NO_OUTREACH."""
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    snap = mode_snapshot()
    snap["disabled_by"] = operator_id or "operator"
    return snap


def is_development_no_outreach() -> bool:
    return get_operating_mode() == MODE_DEVELOPMENT_NO_OUTREACH


def is_controlled_verification() -> bool:
    """True only when mode is controlled AND operator has authorized the session."""
    return (
        get_operating_mode() == MODE_CONTROLLED_REAL_WORLD_VERIFICATION
        and bool(_controlled_authorized_by)
    )


def controlled_session() -> dict[str, Any]:
    return {
        "mode": get_operating_mode(),
        "active": is_controlled_verification(),
        "authorized_by": _controlled_authorized_by,
        "authorized_at": _controlled_authorized_at,
        "acknowledgment": _controlled_acknowledgment,
    }


def outreach_allowed() -> bool:
    """Unrestricted outreach only in OPERATIONAL — never in development or controlled."""
    return get_operating_mode() == MODE_OPERATIONAL


def automatic_external_actions_allowed() -> bool:
    """No mode permits automatic external actions in this build."""
    return False


def classify_action_timing(
    *,
    action_type: str,
    pursuit_state: str | None = None,
) -> str:
    """Map actions to NOW / FUTURE / NONE under current operating mode."""
    from pursuit_qualification_constants import (
        ACTION_TIMING_FUTURE,
        ACTION_TIMING_NONE,
        ACTION_TIMING_NOW,
        PURSUIT_WORTHY,
        PURSUIT_WORTHY_UNCERTAIN,
    )

    outreachish = {
        "CALL_SUPPLIER",
        "EMAIL_SUPPLIER",
        "CALL_FINANCIER",
        "REGISTER_PORTAL",
        "ASK_AGENCY_CLARIFICATION",
        "DOWNLOAD_AUTH_DOCUMENT",
        "OBTAIN_FREIGHT_QUOTE",
        "REVIEW_PG",
        "CONFIRM_COMPLIANCE",
        "REVIEW_BID_DECISION",
    }
    if action_type not in outreachish:
        return ACTION_TIMING_NONE
    if is_development_no_outreach():
        return ACTION_TIMING_FUTURE
    if is_controlled_verification():
        # Controlled: still not automatic NOW — operator must authorize each action
        return ACTION_TIMING_FUTURE
    if pursuit_state in {PURSUIT_WORTHY, PURSUIT_WORTHY_UNCERTAIN}:
        return ACTION_TIMING_NOW
    return ACTION_TIMING_FUTURE


def future_action_phrasing(imperative: str) -> str:
    """Rewrite imperative outreach language for development / controlled modes."""
    if outreach_allowed():
        return imperative
    s = imperative.strip()
    low = s.lower()
    prefix = (
        "Controlled verification if operator-authorized"
        if is_controlled_verification()
        else "Future human verification if pursued"
    )
    if low.startswith("call "):
        return f"{prefix}: {s[5:]}"
    if low.startswith("email "):
        return f"{prefix}: {s[6:]}"
    if "register" in low:
        return f"Portal registration requires operator authorization — not automatic: {s}"
    return f"{prefix} — not an automatic action: {s}"


def mode_snapshot() -> dict[str, Any]:
    return {
        "operating_mode": get_operating_mode(),
        "outreach_allowed": outreach_allowed(),
        "automatic_external_actions_allowed": automatic_external_actions_allowed(),
        "controlled_verification_active": is_controlled_verification(),
        "controlled_session": controlled_session(),
        "emails_sent": 0,
        "calls_placed": 0,
        "supplier_contacts": 0,
        "financier_contacts": 0,
        "agency_contacts": 0,
        "portal_registrations": 0,
        "bids_submitted": 0,
        "financing_applications": 0,
        "signatures": 0,
        "network_transmitted": False,
    }
