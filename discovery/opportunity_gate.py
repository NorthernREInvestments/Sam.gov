"""Structural opportunity validation — reject marketing/navigation rows."""

from __future__ import annotations

import re
from typing import Any

from discovery.deadline import normalize_deadline

_BAD_SOLICITATION_NUMBERS = frozenset(
    {
        "open",
        "closed",
        "awarded",
        "pending",
        "cancelled",
        "canceled",
        "free registration",
        "start browsing now",
        "login",
        "register",
        "sign in",
        "details",
        "type",
        "status",
        "title",
        "unknown",
        "n/a",
        "na",
        "none",
    }
)

_MARKETING_TITLE_PATTERNS = (
    r"^best deal\b",
    r"\bfree registration\b",
    r"\bstart browsing now\b",
    r"\ball the benefits of free registration\b",
    r"\bgain access to\s+\d+",
    r"\bflat membership plans\b",
    r"\bvendor registration\b",
    r"\bselect region\b",
    r"\bselect agency\b",
    r"\bthis website uses cookies\b",
    r"\baccept cookies\b",
    r"^welcome\b",
    r"\bofficial site of the\b",
    r"\bemarketplace\b",
    r"\balternative competitive bidding process policy\b",
    r"^solicitation\s*#?\s*:?\s*$",
    r"^solicitation\s+(search|type|title or description|title)\s*:?\s*$",
    r"^department for this solicitation\s*:?\s*$",
    r"^solicitation/project#\s*:?\s*$",
    r"^department/agency\s*:?\s*$",
    r"^phone number\s*:?\s*$",
    r"^solicitation start date\s*:?\s*$",
    r"^no\. of records per page\b",
    r"^solicitation\s*#\s*$",
    r"^go to solicitation\b",
    r"^solicitation opening (date|time)\s*:?\s*$",
    r"^solicitation tabulations\b",
)

_PROCUREMENT_TITLE_HINTS = (
    r"\brfp\b",
    r"\brfq\b",
    r"\bifb\b",
    r"\brfb\b",
    r"\brfi\b",
    r"\bbid\b",
    r"\bproposal\b",
    r"\bsolicitation\b",
    r"\bprocurement\b",
    r"\bequipment\b",
    r"\bsupplies\b",
    r"\bservices?\b",
    r"\bpurchase\b",
    r"\bcontract\b",
    r"\binvitation\b",
)


def is_garbage_solicitation_number(value: str | None) -> bool:
    if value is None:
        return True
    s = re.sub(r"\s+", " ", str(value)).strip()
    if not s:
        return True
    if len(s) > 80:
        return True
    low = s.lower()
    if low in _BAD_SOLICITATION_NUMBERS:
        return True
    if any(p in low for p in ("free registration", "start browsing", "select region", "login", "register")):
        return True
    if re.fullmatch(r"(open|closed|awarded|pending)", low):
        return True
    # Must look identifier-like: has digit or hyphenated code
    if not re.search(r"\d", s) and not re.search(r"[A-Za-z]{2,}[-_/][A-Za-z0-9]", s):
        return True
    return False


def sanitize_solicitation_number(value: str | None) -> str | None:
    if is_garbage_solicitation_number(value):
        return None
    return re.sub(r"\s+", " ", str(value)).strip()[:120]


def is_garbage_title(title: str | None) -> bool:
    if not title:
        return True
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) < 8:
        return True
    low = t.lower()
    for pat in _MARKETING_TITLE_PATTERNS:
        if re.search(pat, low, re.I):
            return True
    return False


def looks_like_procurement_title(title: str | None) -> bool:
    if not title or is_garbage_title(title):
        return False
    low = re.sub(r"\s+", " ", title).strip().lower()
    if low.startswith("go to "):
        return False
    if re.fullmatch(r"(solicitation|department|phone number)[^a-z0-9]*.*", low) and len(low) < 48:
        return False
    return any(re.search(p, low, re.I) for p in _PROCUREMENT_TITLE_HINTS)


def sanitize_deadline_raw(value: str | None) -> str | None:
    """Return parseable deadline string or None. Keep raw separately for debug."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    if not s or len(s) > 80:
        return None
    low = s.lower()
    if any(
        x in low
        for x in (
            "select region",
            "select agency",
            "start browsing",
            "free registration",
            "alabama",
            "login",
            "register",
        )
    ):
        return None
    parsed = normalize_deadline(s)
    if not parsed.get("parsed_local") and not re.match(r"\d{1,2}/\d{1,2}/\d{4}", s):
        # Month-name forms are handled by normalize_deadline
        if not parsed.get("deadline_raw") or parsed.get("parsed_local") is None:
            return None
    if parsed.get("parsed_local") is None:
        return None
    return s[:120]


def is_structurally_valid_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    """
    Require at least TWO strong opportunity fields before CanonicalOpportunity acceptance.
    """
    reasons: list[str] = []
    strong = 0

    title = (row.get("title") or "").strip()
    sol = row.get("solicitation_number")
    ext = row.get("external_id")
    deadline = row.get("deadline_raw")
    detail = row.get("detail_url")
    agency = row.get("agency")
    status = row.get("status")

    if is_garbage_title(title):
        reasons.append("garbage_or_missing_title")
    elif looks_like_procurement_title(title):
        strong += 1
    else:
        # Generic but non-marketing title counts weakly only with other fields
        reasons.append("title_weak_procurement_signal")

    sol_ok = not is_garbage_solicitation_number(sol)
    if sol_ok:
        strong += 1
    else:
        reasons.append("missing_or_garbage_solicitation_number")

    ext_s = str(ext or "").strip()
    ext_ok = bool(ext_s) and not is_garbage_solicitation_number(ext_s) and len(ext_s) >= 4
    # Stable event IDs like sciquest:1440021 or sourcewell:11364
    if ext_ok and (re.search(r"\d", ext_s) or ":" in ext_s):
        if not sol_ok:
            strong += 1
        elif ext_s.lower() != str(sol or "").lower():
            strong += 1  # distinct stable id
    else:
        reasons.append("missing_or_weak_external_id")

    dl = sanitize_deadline_raw(deadline) if deadline else None
    if dl:
        strong += 1
    elif deadline:
        reasons.append("unparseable_deadline")

    if detail and isinstance(detail, str) and detail.startswith("http") and "register" not in detail.lower():
        strong += 1
    elif detail:
        reasons.append("weak_or_registration_detail_url")

    if agency and len(str(agency).strip()) >= 3 and "select agency" not in str(agency).lower():
        strong += 1

    st = str(status or "").strip().upper()
    if st in {"OPEN", "CLOSED", "AWARDED", "PENDING", "CANCELLED", "CANCELED"}:
        # Status alone is weak; count only with other signals already present
        if strong >= 1:
            strong += 1
    else:
        reasons.append("missing_status")

    valid = (
        strong >= 2
        and not is_garbage_title(title)
        and (looks_like_procurement_title(title) or (sol_ok and dl))
    )
    if not valid and "insufficient_strong_fields" not in reasons:
        reasons.append(f"strong_field_count={strong}<2")

    return {
        "valid": valid,
        "reasons": reasons,
        "strong_field_count": strong,
        "sanitized_solicitation_number": sanitize_solicitation_number(sol),
        "sanitized_deadline_raw": dl,
    }
