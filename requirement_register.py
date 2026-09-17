"""Persistent Requirement Register vocabulary and helpers."""

from __future__ import annotations

from typing import Any

REQ_TYPES = (
    "PRODUCT",
    "QUANTITY",
    "SPECIFICATION",
    "DELIVERY",
    "CHANNEL_AUTHORIZATION",
    "COUNTRY_OF_ORIGIN",
    "BAA",
    "TAA",
    "NMR",
    "SET_ASIDE",
    "CERTIFICATION",
    "LICENSE",
    "BOND",
    "INSURANCE",
    "PAST_PERFORMANCE",
    "PRICING",
    "FORM",
    "SIGNATURE",
    "AMENDMENT_ACKNOWLEDGMENT",
    "REPRESENTATION",
    "TECHNICAL_RESPONSE",
    "OEM_LETTER",
    "SUBMISSION",
    "DEADLINE",
    "OTHER",
)

REQ_VERIFIED_REQUIREMENT = "VERIFIED_REQUIREMENT"
REQ_SATISFIED = "SATISFIED"
REQ_UNSATISFIED = "UNSATISFIED"
REQ_UNKNOWN = "UNKNOWN"
REQ_NOT_APPLICABLE = "NOT_APPLICABLE"

REQ_STATUSES = frozenset(
    {
        REQ_VERIFIED_REQUIREMENT,
        REQ_SATISFIED,
        REQ_UNSATISFIED,
        REQ_UNKNOWN,
        REQ_NOT_APPLICABLE,
    }
)


def requirement_record(
    *,
    requirement_key: str,
    description: str,
    requirement_type: str,
    required: bool = True,
    status: str = REQ_UNKNOWN,
    source_document: str | None = None,
    source_location: str | None = None,
    evidence: str | None = None,
    verified_status: str | None = None,
    owner: str | None = None,
    due_date: str | None = None,
    satisfied_by: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    st = str(status or REQ_UNKNOWN).upper()
    if st not in REQ_STATUSES:
        st = REQ_UNKNOWN
    # Never promote to SATISFIED without explicit satisfied_by / operator evidence
    if st == REQ_SATISFIED and not satisfied_by:
        st = REQ_UNKNOWN
    return {
        "requirement_key": requirement_key,
        "description": description,
        "type": str(requirement_type or "OTHER").upper(),
        "required": bool(required),
        "status": st,
        "source_document": source_document,
        "source_location": source_location,
        "evidence": evidence,
        "verified_status": verified_status,
        "owner": owner,
        "due_date": due_date,
        "satisfied_by": satisfied_by,
        "notes": notes,
    }


def mark_satisfied(req: dict[str, Any], *, satisfied_by: str, evidence: str | None = None) -> dict[str, Any]:
    out = dict(req)
    if not satisfied_by:
        out["status"] = REQ_UNKNOWN
        out["notes"] = "cannot_satisfy_without_satisfied_by"
        return out
    out["status"] = REQ_SATISFIED
    out["satisfied_by"] = satisfied_by
    if evidence:
        out["evidence"] = evidence
    return out


def unresolved_mandatory(requirements: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for r in requirements or []:
        if not r.get("required"):
            continue
        if r.get("status") in {REQ_SATISFIED, REQ_NOT_APPLICABLE}:
            continue
        out.append(r)
    return out
