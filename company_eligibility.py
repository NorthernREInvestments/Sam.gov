"""Configurable company eligibility for Stage 0 set-aside checks.

Update COMPANY_CERTIFICATIONS / COMPANY_UNSUPPORTED_SET_ASIDES when certs change.
No OpenAI calls.
"""

from __future__ import annotations

import os
import re
from typing import Any

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

# Certifications the company currently holds (comma-separated codes).
# Default: general small business eligible — not WOSB/SDVOSB/etc.
_DEFAULT_HELD = "SB,SMALL_BUSINESS,TOTAL_SMALL_BUSINESS,SBA"

# Set-asides that require certs the company does NOT hold (configurable).
_DEFAULT_UNSUPPORTED = "WOSB,EDWOSB,SDVOSB,VOSB,HUBZONE,HUB Zone,8(a),8A,8a"


def held_certifications() -> set[str]:
    raw = os.getenv("COMPANY_CERTIFICATIONS", _DEFAULT_HELD)
    return {_norm_token(x) for x in raw.split(",") if x.strip()}


def unsupported_set_aside_patterns() -> list[str]:
    raw = os.getenv("COMPANY_UNSUPPORTED_SET_ASIDES", _DEFAULT_UNSUPPORTED)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _norm_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def normalize_set_aside(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# Patterns that mean unrestricted / total SB — NEVER auto-reject
_ALWAYS_OK = (
    r"\bunrestricted\b",
    r"\bno\s*set[\s-]*aside\b",
    r"\bnone\b",
    r"\btotal\s+small\s+business\b",
    r"\bsmall\s+business\s+set[\s-]*aside\b",
    r"\bsba\b",
    r"\bsb\b",
    r"\bsmall\s+business\b",
)


def set_aside_eligibility(raw: Any) -> dict[str, Any]:
    """
    Returns:
      eligible: True | False | None (unknown)
      reason: code or None
      matched: matched unsupported label if any
    """
    text = normalize_set_aside(raw)
    if not text:
        return {"eligible": None, "reason": "unknown_set_aside", "matched": None}

    lower = text.lower()
    for pat in _ALWAYS_OK:
        if re.search(pat, lower, re.I):
            return {"eligible": True, "reason": "allowed_set_aside", "matched": None}

    for label in sorted(unsupported_set_aside_patterns(), key=len, reverse=True):
        # Word-ish match: avoid matching "SB" inside "WOSB" incorrectly — check label as unit
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])", text, re.I):
            # If company somehow holds it, allow
            if _norm_token(label) in held_certifications():
                return {"eligible": True, "reason": "held_certification", "matched": label}
            return {"eligible": False, "reason": "unsupported_certification", "matched": label}

    # Unknown specific set-aside text — do not reject
    return {"eligible": None, "reason": "unrecognized_set_aside", "matched": None}
