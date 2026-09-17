"""Deadline conflict detection and authority-based reconciliation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from public_evidence_constants import (
    DEADLINE_AMENDMENT,
    DEADLINE_CONFLICT_UNRESOLVED,
    DEADLINE_DIFFERENT_EVENT,
    DEADLINE_OPEN_VS_CLOSE_CLARIFIED,
    DEADLINE_PARSE_ERROR,
    DEADLINE_RESOLVED,
    DEADLINE_STALE_MIRROR,
    EV_AUTHORITATIVE_CURRENT,
    EV_THIRD_PARTY_MIRROR,
)
from public_evidence_models import evidence_fact


def parse_deadline_candidate(raw: str | None) -> dict[str, Any]:
    """Parse common deadline strings into comparable parts."""
    if not raw:
        return {"raw": None, "date": None, "time": None, "tz": None, "ok": False}
    s = re.sub(r"\s+", " ", str(raw)).strip()
    m = re.search(
        r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})(?:[,\s]+(\d{1,2}:\d{2}\s*[AP]M))?\s*([A-Z]{2,4})?",
        s,
        re.I,
    )
    if not m:
        # Month name form
        m2 = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
            s,
            re.I,
        )
        if not m2:
            return {"raw": s, "date": None, "time": None, "tz": None, "ok": False}
        months = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        mo = months[m2.group(1).lower()]
        d = int(m2.group(2))
        y = int(m2.group(3))
        return {
            "raw": s,
            "date": f"{y:04d}-{mo:02d}-{d:02d}",
            "time": None,
            "tz": None,
            "ok": True,
            "sort_key": f"{y:04d}-{mo:02d}-{d:02d}",
        }
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Ambiguous MDY assumed for US procurement
    return {
        "raw": s,
        "date": f"{y:04d}-{mo:02d}-{d:02d}",
        "time": (m.group(4) or "").strip() or None,
        "tz": (m.group(5) or "").strip().upper() or None,
        "ok": True,
        "sort_key": f"{y:04d}-{mo:02d}-{d:02d}",
    }


def deadline_evidence_record(
    *,
    raw: str,
    role: str,
    evidence_class: str,
    source_url: str | None = None,
    document: str | None = None,
    page_section: str | None = None,
    confidence: str = "MEDIUM",
) -> dict[str, Any]:
    parsed = parse_deadline_candidate(raw)
    fact = evidence_fact(
        raw,
        evidence_class=evidence_class,
        source_url=source_url,
        document=document,
        page_section=page_section,
        confidence=confidence,
        relationship_to_current="current_solicitation_deadline_candidate",
        authoritative_for_bidding=(evidence_class == EV_AUTHORITATIVE_CURRENT and role == "CLOSE"),
    )
    return {
        **fact,
        "role": role,  # OPEN | CLOSE | UNKNOWN | MIRROR_CLAIMED_DEADLINE
        "parsed": parsed,
    }


def reconcile_deadline_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Reconcile conflicting deadlines with provenance.
    Authority: formal current agency CLOSE > amendment > open date ≠ close >
    third-party mirrors.
    Unresolved → DEADLINE_CONFLICT_UNRESOLVED and earliest credible operational date.
    """
    if not records:
        return {
            "status": DEADLINE_CONFLICT_UNRESOLVED,
            "best_supported_close": None,
            "operational_deadline": None,
            "operational_deadline_basis": "none",
            "records": [],
            "resolution_notes": ["no_deadline_evidence"],
            "conflict": False,
        }

    closes = [r for r in records if r.get("role") == "CLOSE" and (r.get("parsed") or {}).get("ok")]
    opens = [r for r in records if r.get("role") == "OPEN" and (r.get("parsed") or {}).get("ok")]
    mirrors = [
        r
        for r in records
        if r.get("role") in {"UNKNOWN", "MIRROR_CLAIMED_DEADLINE"}
        and (r.get("parsed") or {}).get("ok")
    ]

    auth_closes = [r for r in closes if r.get("evidence_class") == EV_AUTHORITATIVE_CURRENT]
    notes: list[str] = []

    # Detect Open vs Close confusion: mirror date equals authoritative OPEN
    if auth_closes and opens and mirrors:
        open_dates = {(r.get("parsed") or {}).get("date") for r in opens}
        close_dates = {(r.get("parsed") or {}).get("date") for r in auth_closes}
        mirror_dates = {(r.get("parsed") or {}).get("date") for r in mirrors}
        if mirror_dates & open_dates and mirror_dates.isdisjoint(close_dates):
            best = auth_closes[0]
            return {
                "status": DEADLINE_OPEN_VS_CLOSE_CLARIFIED,
                "best_supported_close": best.get("value"),
                "operational_deadline": best.get("value"),
                "operational_deadline_basis": "authoritative_close_over_mirror_open_confusion",
                "records": records,
                "resolution_notes": [
                    "third_party_mirror_date_matches_authoritative_OPEN_not_CLOSE",
                    "authoritative_CLOSE_retained",
                ],
                "conflict": True,
                "conflict_resolved": True,
                "earliest_credible": _earliest([*auth_closes, *mirrors]),
            }

    # Distinct close candidates across classes
    close_by_date: dict[str, list[dict[str, Any]]] = {}
    for r in closes + [m for m in mirrors if m.get("role") == "MIRROR_CLAIMED_DEADLINE"]:
        d = (r.get("parsed") or {}).get("date")
        if d:
            close_by_date.setdefault(d, []).append(r)

    distinct_dates = list(close_by_date.keys())
    if len(distinct_dates) <= 1 and auth_closes:
        best = auth_closes[0]
        return {
            "status": DEADLINE_RESOLVED,
            "best_supported_close": best.get("value"),
            "operational_deadline": best.get("value"),
            "operational_deadline_basis": "single_authoritative_close",
            "records": records,
            "resolution_notes": notes or ["single_close_date"],
            "conflict": False,
            "conflict_resolved": True,
            "earliest_credible": best.get("value"),
        }

    if len(distinct_dates) > 1:
        # Prefer authoritative current close if present among conflicts
        if auth_closes:
            auth_dates = {(r.get("parsed") or {}).get("date") for r in auth_closes}
            mirror_only = [d for d in distinct_dates if d not in auth_dates]
            if mirror_only and len(auth_dates) == 1:
                best = auth_closes[0]
                return {
                    "status": DEADLINE_STALE_MIRROR,
                    "best_supported_close": best.get("value"),
                    "operational_deadline": best.get("value"),
                    "operational_deadline_basis": "authoritative_close_over_stale_or_wrong_mirror",
                    "records": records,
                    "resolution_notes": [
                        "conflicting_mirror_dates_demoted",
                        f"mirror_only_dates={mirror_only}",
                    ],
                    "conflict": True,
                    "conflict_resolved": True,
                    "earliest_credible": _earliest([*auth_closes, *mirrors]),
                }

        # Unresolved: use earliest credible among all parsed
        earliest = _earliest(closes + mirrors + auth_closes)
        return {
            "status": DEADLINE_CONFLICT_UNRESOLVED,
            "best_supported_close": (auth_closes[0].get("value") if auth_closes else None),
            "operational_deadline": earliest,
            "operational_deadline_basis": "conservative_earliest_credible_until_resolved",
            "records": records,
            "resolution_notes": [
                "multiple_distinct_deadline_dates",
                "must_not_rely_on_later_date_alone",
            ],
            "conflict": True,
            "conflict_resolved": False,
            "earliest_credible": earliest,
            "distinct_dates": distinct_dates,
        }

    # Only mirrors / opens
    if auth_closes:
        best = auth_closes[0]
        return {
            "status": DEADLINE_RESOLVED,
            "best_supported_close": best.get("value"),
            "operational_deadline": best.get("value"),
            "operational_deadline_basis": "authoritative_close",
            "records": records,
            "resolution_notes": notes,
            "conflict": False,
            "conflict_resolved": True,
            "earliest_credible": best.get("value"),
        }

    earliest = _earliest(records)
    return {
        "status": DEADLINE_CONFLICT_UNRESOLVED,
        "best_supported_close": None,
        "operational_deadline": earliest,
        "operational_deadline_basis": "no_authoritative_close_conservative_earliest",
        "records": records,
        "resolution_notes": ["no_authoritative_current_close"],
        "conflict": True,
        "conflict_resolved": False,
        "earliest_credible": earliest,
    }


def _earliest(recs: list[dict[str, Any]]) -> str | None:
    dated = []
    for r in recs:
        p = r.get("parsed") or {}
        if p.get("ok") and p.get("sort_key"):
            dated.append((p["sort_key"], r.get("value")))
    if not dated:
        return None
    dated.sort(key=lambda x: x[0])
    return dated[0][1]


def extract_open_close_from_sciquest_text(
    text: str,
    *,
    source_url: str | None = None,
    document: str | None = None,
) -> list[dict[str, Any]]:
    """Extract Open/Close timestamps from SciQuest event PDF/listing text."""
    out: list[dict[str, Any]] = []
    m_open = re.search(r"\bOpen\s*\n?\s*([0-9/]+,\s*[0-9:]+\s*[AP]M\s*[A-Z]{2,4})", text or "", re.I)
    if m_open:
        out.append(
            deadline_evidence_record(
                raw=m_open.group(1).strip(),
                role="OPEN",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                source_url=source_url,
                document=document,
                page_section="Open",
                confidence="HIGH",
            )
        )
    m_close = re.search(r"\bClose\s*\n?\s*([0-9/]+,\s*[0-9:]+\s*[AP]M\s*[A-Z]{2,4})", text or "", re.I)
    if not m_close:
        m_close = re.search(
            r"Sealed Until\s*\n?\s*([0-9/]+,\s*[0-9:]+\s*[AP]M\s*[A-Z]{2,4})",
            text or "",
            re.I,
        )
    if m_close:
        out.append(
            deadline_evidence_record(
                raw=m_close.group(1).strip(),
                role="CLOSE",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                source_url=source_url,
                document=document,
                page_section="Close/Sealed Until",
                confidence="HIGH",
            )
        )
    return out
