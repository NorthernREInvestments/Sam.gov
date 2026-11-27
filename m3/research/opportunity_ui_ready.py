"""Hard gate: incomplete opportunities must not appear as Active/Saved UI rows.

Unknown Closes = FAIL. A row is UI-ready only when the operator can act on it
with a real solicitation id, official link, and closing date.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_JUNK_TITLE_RE = re.compile(
    r"(?i)(?:^\s*(?:word|excel|external|office\s+depot\s+contract)\s*$"
    r"|\bNebraska DAS procurement \(public site\) - (?:Word|Excel)\b"
    r"|\bOffice Depot Contract\b)"
)

_BAD_SOL_RE = re.compile(
    r"(?i)interested\s+suppliers|addendum|http://|https://|\.pdf\b"
)


def parse_deadline_value(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s or s.lower() in {"unknown", "n/a", "none", "null", "tbd"}:
        return None
    s = s.replace("Z", "+00:00")
    # Strip timezone labels (CT/ET/PT/…) so strptime can see the clock time.
    s_clock = re.sub(
        r"(?i)\s*\(?\b(?:UTC|GMT|[ECMP][DS]?T|Central|Eastern|Pacific|Mountain)(?:\s+Time)?\)?\s*$",
        "",
        s,
    ).strip()
    for cand in (s, s_clock, s[:25], s[:19], s[:10]):
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        for candidate in (s_clock[:40], s[:40]):
            try:
                dt = datetime.strptime(candidate, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    # "January 5, 2027 at 2:00 PM"
    m = re.search(
        r"(?i)(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        s,
    )
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
        except Exception:
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            except Exception:
                dt = None
        if dt:
            return dt.replace(tzinfo=timezone.utc)
    return None


def solicitation_ok(sol: Any) -> bool:
    s = str(sol or "").strip()
    if not s or len(s) < 3:
        return False
    if len(s) > 64:
        return False
    if _BAD_SOL_RE.search(s):
        return False
    # Title fragments used as ids (Providence fallback)
    if s.count(" ") >= 4:
        return False
    return True


def title_ok(title: Any) -> bool:
    t = str(title or "").strip()
    if len(t) < 4:
        return False
    if _JUNK_TITLE_RE.search(t):
        return False
    return True


def opportunity_ui_ready(item: dict[str, Any] | Any) -> tuple[bool, list[str]]:
    """Return (ok, fail_reasons). Unknown closes / bad id / junk title = FAIL."""
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    reasons: list[str] = []
    title = get("title") or get("display_title")
    if not title_ok(title):
        reasons.append("junk_or_missing_title")
    sol = get("solicitation_number")
    if not solicitation_ok(sol):
        reasons.append("bad_or_missing_solicitation")
    url = get("official_url")
    if not str(url or "").strip():
        reasons.append("missing_official_url")
    deadline = get("deadline_at")
    if parse_deadline_value(deadline) is None:
        reasons.append("unknown_closes")
    return (not reasons, reasons)


def enrich_deadline_from_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Fill deadline_at from triage / doc-read close_date when listing omitted it."""
    out = dict(item)
    if parse_deadline_value(out.get("deadline_at")) is not None:
        return out
    triage = out.get("triage") or {}
    doc_final = ((out.get("doc_read") or {}).get("final") or {})
    for cand in (
        triage.get("close_date"),
        doc_final.get("close_date"),
        out.get("response_deadline"),
        (out.get("evidence") or {}).get("close_date"),
    ):
        dt = parse_deadline_value(cand)
        if dt is not None:
            out["deadline_at"] = dt.isoformat()
            break
    return out
