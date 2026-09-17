"""Compliance matrix + company capability mapping for bid requirements."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from bid_compliance_constants import (
    CO_FUTURE,
    CO_KNOWN_SATISFIED,
    CO_KNOWN_UNSATISFIED,
    CO_UNKNOWN,
    FUTURE_ACTION,
    SEV_HARD_BLOCKER,
    SEV_MANDATORY_MATERIAL,
    ST_BLOCKED,
    ST_FUTURE,
    ST_LIKELY,
    ST_NA,
    ST_SATISFIED,
    ST_UNRESOLVED,
    ST_UNSATISFIED,
)
from company_profile import (
    CAP_NOT_HELD,
    CAP_UNKNOWN,
    CAP_VERIFIED,
    KEY_BONDING_CAPACITY,
    KEY_CERTIFICATIONS,
    KEY_FEDERAL_PAST_PERFORMANCE,
    KEY_SPECIAL_LICENSES,
    get_capability,
    normalize_profile,
)


# Map requirement categories → company capability keys / facts
_CATEGORY_CAPABILITY = {
    "BOND": KEY_BONDING_CAPACITY,
    "LICENSE": KEY_SPECIAL_LICENSES,
    "CERTIFICATION": KEY_CERTIFICATIONS,
    "PAST_PERFORMANCE": KEY_FEDERAL_PAST_PERFORMANCE,
    "EXPERIENCE": KEY_FEDERAL_PAST_PERFORMANCE,
}


def map_company_to_requirement(
    requirement: dict[str, Any],
    company_profile: dict[str, Any] | None,
    *,
    company_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Map known company evidence. UNKNOWN stays UNKNOWN.
    Never marks SATISFIED because something is 'usually easy'.
    """
    profile = normalize_profile(company_profile)
    facts = company_facts or {}
    cat = str(requirement.get("category") or "")
    mapping = {
        "company_state": CO_UNKNOWN,
        "status": ST_UNRESOLVED,
        "evidence": None,
        "blocking": False,
        "automation_possible": False,
        "future_human_action": False,
        "resolution_path": "research_or_operator",
    }

    # Explicit company facts (UEI, CAGE, SAM, etc.) — only if provided
    fact_keys = {
        "SAM_REGISTRATION": "sam_registered",
        "NAICS": "naics_codes",
        "SMALL_BUSINESS": "small_business",
        "SET_ASIDE": "set_aside_eligible",
        "SIGNATURE": "signature_authority",
        "PORTAL_REGISTRATION": "portal_registrations",
        "STATE_VENDOR_REGISTRATION": "state_vendor_registered",
    }
    if cat in fact_keys:
        fk = fact_keys[cat]
        if fk not in facts:
            mapping["company_state"] = CO_UNKNOWN
            mapping["status"] = ST_FUTURE if cat in {
                "PORTAL_REGISTRATION",
                "STATE_VENDOR_REGISTRATION",
                "SIGNATURE",
            } else ST_UNRESOLVED
            mapping["future_human_action"] = True
            mapping["operator_action"] = FUTURE_ACTION
            mapping["resolution_path"] = FUTURE_ACTION
            return mapping
        val = facts[fk]
        if val is True or (isinstance(val, (list, set)) and len(val) > 0):
            mapping["company_state"] = CO_KNOWN_SATISFIED
            mapping["status"] = ST_SATISFIED
            mapping["evidence"] = {"fact": fk, "value": val}
            return mapping
        if val is False:
            mapping["company_state"] = CO_KNOWN_UNSATISFIED
            mapping["status"] = ST_UNSATISFIED
            if requirement.get("severity") == SEV_HARD_BLOCKER:
                mapping["blocking"] = True
                mapping["status"] = ST_BLOCKED
            mapping["evidence"] = {"fact": fk, "value": val}
            return mapping
        mapping["company_state"] = CO_UNKNOWN
        return mapping

    cap_key = _CATEGORY_CAPABILITY.get(cat)
    if cap_key:
        cap = get_capability(profile, cap_key)
        st = str(cap.get("status") or CAP_UNKNOWN)
        if st == CAP_VERIFIED and cap.get("held"):
            mapping["company_state"] = CO_KNOWN_SATISFIED
            mapping["status"] = ST_SATISFIED
            mapping["evidence"] = {"capability": cap_key, "status": st}
            return mapping
        if st == CAP_NOT_HELD:
            mapping["company_state"] = CO_KNOWN_UNSATISFIED
            mapping["status"] = ST_UNSATISFIED
            if requirement.get("severity") in {SEV_HARD_BLOCKER, SEV_MANDATORY_MATERIAL}:
                mapping["blocking"] = True
                mapping["status"] = ST_BLOCKED
            mapping["evidence"] = {"capability": cap_key, "status": st}
            mapping["future_human_action"] = True
            mapping["operator_action"] = FUTURE_ACTION
            return mapping
        # UNKNOWN capability
        mapping["company_state"] = CO_UNKNOWN
        mapping["status"] = ST_UNRESOLVED
        mapping["future_human_action"] = True
        mapping["operator_action"] = FUTURE_ACTION
        return mapping

    # Default: unresolved / future if operator-facing
    if requirement.get("operator_action") == FUTURE_ACTION:
        mapping["company_state"] = CO_FUTURE
        mapping["status"] = ST_FUTURE
        mapping["future_human_action"] = True
        mapping["operator_action"] = FUTURE_ACTION
        mapping["resolution_path"] = FUTURE_ACTION
    return mapping


def build_compliance_matrix(
    requirements: list[dict[str, Any]],
    *,
    company_profile: dict[str, Any] | None = None,
    company_facts: dict[str, Any] | None = None,
    product_states: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = []
    product_states = product_states or {}
    for req in requirements:
        mapped = map_company_to_requirement(req, company_profile, company_facts=company_facts)
        cat = req.get("category")
        if cat in product_states:
            ps = product_states[cat]
            mapped["product_compliance"] = ps
            if ps.get("state") in {"NONCOMPLIANT"}:
                mapped["status"] = ST_UNSATISFIED
                mapped["blocking"] = True
            elif ps.get("state") in {"EXACT_MATCH", "COMPLIANT_EQUIVALENT"} and mapped["status"] == ST_UNRESOLVED:
                mapped["status"] = ST_LIKELY

        if not req.get("mandatory") and mapped["status"] == ST_UNRESOLVED:
            # Informational — not a blocker
            mapped["status"] = ST_NA if req.get("severity") == "INFORMATIONAL" else mapped["status"]

        row = {
            "kind": "ComplianceMatrixRow",
            "requirement_id": req.get("requirement_id"),
            "requirement": req.get("normalized_requirement"),
            "category": cat,
            "source": req.get("source_document") or req.get("source_document_id"),
            "source_snippet": req.get("source_snippet"),
            "mandatory": bool(req.get("mandatory")),
            "severity": req.get("severity"),
            "current_status": mapped["status"],
            "company_state": mapped["company_state"],
            "evidence": mapped.get("evidence") or req.get("evidence"),
            "confidence": req.get("confidence"),
            "blocking": bool(mapped.get("blocking")),
            "resolution_path": mapped.get("resolution_path"),
            "automation_possible": bool(mapped.get("automation_possible")),
            "future_human_action": bool(mapped.get("future_human_action")),
            "operator_action": mapped.get("operator_action") or req.get("operator_action"),
            "last_verified": now_utc().isoformat(),
            "product_compliance": mapped.get("product_compliance"),
        }
        rows.append(row)

    blockers = [r for r in rows if r.get("blocking") or r.get("current_status") == ST_BLOCKED]
    unresolved = [
        r
        for r in rows
        if r.get("mandatory")
        and r.get("current_status")
        in {ST_UNRESOLVED, ST_FUTURE, "RESEARCH_REQUIRED", "DOCUMENT_REQUIRED", "CONFLICTING", ST_UNSATISFIED}
    ]
    satisfied = [r for r in rows if r.get("current_status") in {ST_SATISFIED, ST_LIKELY}]
    future_actions = [r for r in rows if r.get("operator_action") == FUTURE_ACTION or r.get("current_status") == ST_FUTURE]

    return {
        "kind": "ComplianceMatrix",
        "rows": rows,
        "counts": {
            "total": len(rows),
            "mandatory": sum(1 for r in rows if r.get("mandatory")),
            "satisfied": len(satisfied),
            "unresolved": len(unresolved),
            "blockers": len(blockers),
            "future_actions": len(future_actions),
        },
        "blockers": blockers,
        "unresolved_mandatory": unresolved,
        "future_actions": future_actions,
        "hard_blocker_present": any(r.get("severity") == SEV_HARD_BLOCKER and r.get("blocking") for r in rows),
    }
