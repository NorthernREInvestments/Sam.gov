"""BidNet closing-date extraction and persistence helpers.

Used by discovery detail enrichment and isolated recovery. Never invents dates.
Preserves raw portal text, timezone, and retrieval timestamp.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from bidnet_recovery.parse_abstract import parse_bidnet_abstract
from discovery.deadline import normalize_deadline

BUILD = "20261010-m3-bidnet-deadline-persist-v1"

_NOT_STATED = re.compile(
    r"^(not\s*stated|n/?a|none|unknown|tbd|to\s*be\s*determined)[\s.]*$",
    re.I,
)


def extract_bidnet_closing_from_html(
    html: str,
    *,
    detail_url: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Parse Closing Date from BidNet abstract/detail HTML."""
    parsed = parse_bidnet_abstract(html or "", detail_url=detail_url)
    raw = parsed.get("deadline_raw") or parsed.get("close_date_raw")
    retrieved_at = retrieved_at or now_utc().isoformat()

    if raw is None or str(raw).strip() == "":
        return {
            "ok": False,
            "deadline_raw": None,
            "deadline": None,
            "not_stated": False,
            "timezone": None,
            "timezone_confidence": "UNKNOWN",
            "source_url": detail_url,
            "retrieved_at": retrieved_at,
            "reason": "CLOSING_DATE_FIELD_MISSING",
            "portal_status_hints": _status_hints(html or ""),
            "parse": parsed,
            "build": BUILD,
        }

    raw_s = str(raw).strip()
    if _NOT_STATED.match(raw_s):
        return {
            "ok": True,
            "deadline_raw": raw_s,
            "deadline": None,
            "not_stated": True,
            "timezone": None,
            "timezone_confidence": "UNKNOWN",
            "source_url": detail_url,
            "retrieved_at": retrieved_at,
            "reason": "CLOSING_NOT_STATED",
            "portal_status_hints": _status_hints(html or ""),
            "parse": parsed,
            "build": BUILD,
        }

    nd = normalize_deadline(raw_s)
    return {
        "ok": bool(nd.get("utc_deadline") or nd.get("parsed_local")),
        "deadline_raw": raw_s,
        "deadline": nd.get("utc_deadline") or nd.get("parsed_local"),
        "parsed_local": nd.get("parsed_local"),
        "timezone": nd.get("timezone"),
        "timezone_confidence": nd.get("timezone_confidence"),
        "date_only": nd.get("date_only"),
        "deadline_passed": nd.get("deadline_passed"),
        "not_stated": False,
        "source_url": detail_url,
        "retrieved_at": retrieved_at,
        "reason": "OK" if (nd.get("utc_deadline") or nd.get("parsed_local")) else "NORMALIZE_FAILED",
        "portal_status_hints": _status_hints(html or ""),
        "normalize": nd,
        "parse": parsed,
        "build": BUILD,
    }


def apply_deadline_to_row(
    row: dict[str, Any],
    extracted: dict[str, Any],
    *,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """Non-destructive merge of extracted deadline into an opportunity row.

    Never overwrites a valid existing deadline unless overwrite_existing=True.
    Always records recovery provenance in raw_metadata.
    """
    out = dict(row)
    meta = dict(out.get("raw_metadata") or {})
    existing = out.get("deadline")
    existing_raw = out.get("deadline_raw")

    has_valid_existing = bool(existing) and not (
        isinstance(existing_raw, str) and _NOT_STATED.match(existing_raw.strip())
    )

    applied = False
    if extracted.get("not_stated"):
        if not has_valid_existing or overwrite_existing:
            out["deadline"] = None
            out["deadline_raw"] = extracted.get("deadline_raw") or "Not Stated"
            applied = True
        action = "SET_NOT_STATED" if applied else "PRESERVED_EXISTING"
    elif extracted.get("deadline"):
        if not has_valid_existing or overwrite_existing:
            out["deadline"] = extracted["deadline"]
            out["deadline_raw"] = extracted.get("deadline_raw")
            if extracted.get("timezone"):
                out["deadline_timezone"] = extracted["timezone"]
            if extracted.get("timezone_confidence"):
                out["deadline_tz_confidence"] = extracted["timezone_confidence"]
            applied = True
        action = "SET_DEADLINE" if applied else "PRESERVED_EXISTING"
    else:
        action = "NO_DEADLINE_EXTRACTED"

    meta["bidnet_deadline_recovery"] = {
        "build": BUILD,
        "action": action,
        "applied": applied,
        "deadline_raw": extracted.get("deadline_raw"),
        "deadline": extracted.get("deadline"),
        "not_stated": extracted.get("not_stated"),
        "timezone": extracted.get("timezone"),
        "timezone_confidence": extracted.get("timezone_confidence"),
        "source_url": extracted.get("source_url"),
        "retrieved_at": extracted.get("retrieved_at"),
        "reason": extracted.get("reason"),
        "portal_status_hints": extracted.get("portal_status_hints"),
    }
    out["raw_metadata"] = meta
    return out


def _status_hints(html: str) -> list[str]:
    hints = []
    low = html.lower()
    for label in ("cancelled", "canceled", "awarded", "closed", "expired", "open", "accepting"):
        if re.search(rf"\b{label}\b", low):
            hints.append(label)
    if "not stated" in low:
        hints.append("not_stated_text")
    return sorted(set(hints))[:12]
