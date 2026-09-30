"""Normalized ExecutionRequirement records."""

from __future__ import annotations

import hashlib
from typing import Any

from application_clock import now_utc
from execution_requirements.constants import (
    ACTOR_VA,
    CATEGORIES,
    REQUIREMENT_STATUSES,
    ST_UNKNOWN,
)


def _req_id(category: str, raw: str, subtype: str = "") -> str:
    h = hashlib.sha256(f"{category}|{subtype}|{raw[:160]}".encode()).hexdigest()[:12]
    return f"EXR-{category[:10]}-{h}"


def empty_requirement(**overrides: Any) -> dict[str, Any]:
    """Canonical ExecutionRequirement — Unknown never defaults to satisfied."""
    base = {
        "kind": "ExecutionRequirement",
        "requirement_id": "EXR-UNKNOWN",
        "category": "OTHER",
        "subtype": None,
        "raw_text": None,
        "normalized_requirement": "UNKNOWN",
        "source_document": "UNKNOWN",
        "source_page_or_section": "UNKNOWN",
        "mandatory": False,
        "conditional": False,
        "status": ST_UNKNOWN,
        "assigned_actor": ACTOR_VA,
        "due_event": None,
        "due_date": None,
        "blocking": False,
        "confidence": "UNKNOWN",
        "evidence": [],
        "notes": None,
        "plain_english_action": None,
        "clause_relevance": None,
        "what_this_requires": None,
        "clin": None,
        "captured_value": None,
        "extracted_at": now_utc().isoformat(),
    }
    base.update(overrides)
    cat = str(base.get("category") or "OTHER").upper()
    if cat not in CATEGORIES:
        cat = "OTHER"
    base["category"] = cat
    st = str(base.get("status") or ST_UNKNOWN).upper()
    if st not in REQUIREMENT_STATUSES:
        st = ST_UNKNOWN
    # Never silently promote UNKNOWN → CONFIRMED
    if st == "CONFIRMED" and not (base.get("evidence") or base.get("captured_value")):
        st = ST_UNKNOWN
        base["notes"] = (base.get("notes") or "") + ";denied_confirmed_without_evidence"
    base["status"] = st
    if base.get("requirement_id") in (None, "", "EXR-UNKNOWN"):
        base["requirement_id"] = _req_id(
            cat,
            str(base.get("raw_text") or base.get("normalized_requirement") or ""),
            str(base.get("subtype") or ""),
        )
    return base


def make_requirement(
    *,
    category: str,
    normalized: str,
    raw_text: str | None = None,
    subtype: str | None = None,
    source_document: str | None = None,
    source_page_or_section: str | None = None,
    mandatory: bool = False,
    conditional: bool = False,
    status: str = ST_UNKNOWN,
    assigned_actor: str = ACTOR_VA,
    due_event: str | None = None,
    due_date: str | None = None,
    blocking: bool = False,
    confidence: str = "EXTRACTED",
    evidence: list[dict[str, Any]] | None = None,
    notes: str | None = None,
    plain_english_action: str | None = None,
    clause_relevance: str | None = None,
    what_this_requires: str | None = None,
    clin: str | None = None,
    captured_value: Any = None,
) -> dict[str, Any]:
    ev = list(evidence or [])
    if raw_text and not any(e.get("raw_text") == raw_text for e in ev if isinstance(e, dict)):
        ev.append(
            {
                "source_document": source_document or "UNKNOWN",
                "source_page_or_section": source_page_or_section or "UNKNOWN",
                "raw_text": (raw_text or "")[:500],
                "confidence": confidence,
            }
        )
    return empty_requirement(
        category=category,
        subtype=subtype,
        raw_text=(raw_text or "")[:500] if raw_text else None,
        normalized_requirement=normalized,
        source_document=source_document or "UNKNOWN",
        source_page_or_section=source_page_or_section or "UNKNOWN",
        mandatory=bool(mandatory),
        conditional=bool(conditional),
        status=status,
        assigned_actor=assigned_actor,
        due_event=due_event,
        due_date=due_date,
        blocking=bool(blocking) and bool(mandatory),
        confidence=confidence,
        evidence=ev,
        notes=notes,
        plain_english_action=plain_english_action,
        clause_relevance=clause_relevance,
        what_this_requires=what_this_requires,
        clin=clin,
        captured_value=captured_value,
    )
