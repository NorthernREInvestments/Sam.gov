"""Canonical operator enrichment path — single truth for all surfaces.

base deal data
→ execution/compliance enrichment
→ execution-critical blockers
→ supplier/economics/financing resolution
→ owner gate
→ operator workflow projection
→ operator summary

Authority for READY FOR OWNER APPROVAL:
  execution_requirements.readiness.evaluate_owner_approval_gate
  → owner_approval_gate.ready_for_owner_approval

Legacy fields (readiness_summary, lifecycle READY, BID_PREPARATION, allPass)
must never independently create owner-ready status.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from execution_requirements.constants import BUILD_TAG as EXEC_BUILD
from execution_requirements.profile import build_execution_compliance_profile
from execution_requirements.supplier_state import resolve_supplier_execution_state


def enrich_deal_for_operator(
    row: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    amendment_text: str | None = None,
    include_summary: bool = True,
    include_full_profile: bool = True,
) -> dict[str, Any]:
    """
    Canonical enrichment used by Dashboard, New Opportunities, Deep Dive,
    Pipeline, History, mobile API summaries, and portfolio summaries.
    """
    base = dict(row) if isinstance(row, dict) else {}
    solicitation = (
        text
        or amendment_text
        or base.get("solicitation_text")
        or base.get("description")
        or base.get("evidence_text_excerpt")
        or ""
    )
    amd = amendment_text or base.get("amendment_text")

    profile = build_execution_compliance_profile(
        base,
        text=solicitation or None,
        amendment_text=amd,
    )

    enriched = deepcopy(base)
    enriched["execution_compliance"] = profile if include_full_profile else {
        "kind": profile.get("kind"),
        "build": profile.get("build"),
        "owner_approval_gate": profile.get("owner_approval_gate"),
        "execution_critical_blockers": profile.get("execution_critical_blockers"),
        "ready_for_owner_approval": profile.get("ready_for_owner_approval"),
        "va_next_actions": profile.get("va_next_actions"),
        "supplier_confirmation_checklist": profile.get("supplier_confirmation_checklist"),
        "submission_checklist": profile.get("submission_checklist"),
        "cash_cycle": profile.get("cash_cycle"),
        "deep_dive_sections": profile.get("deep_dive_sections"),
    }
    gate = profile.get("owner_approval_gate") or {}
    enriched["owner_approval_gate"] = gate
    enriched["execution_critical_blockers"] = list(profile.get("execution_critical_blockers") or [])
    # Sole authoritative READY flag
    enriched["ready_for_owner_approval"] = bool(gate.get("ready_for_owner_approval")) is True and bool(
        profile.get("ready_for_owner_approval")
    )
    # Force consistency: profile.ready mirrors gate
    enriched["ready_for_owner_approval"] = bool(gate.get("ready_for_owner_approval"))

    supplier_state = gate.get("supplier_execution_state") or resolve_supplier_execution_state(enriched)
    enriched["supplier_execution_state"] = supplier_state

    # Feed blockers into workflow projection input
    proj_row = dict(enriched)
    proj_row["execution_critical_blockers"] = enriched["execution_critical_blockers"]
    proj_row["ready_for_owner_approval"] = enriched["ready_for_owner_approval"]

    try:
        from operator_workflow.summary import attach_operator_workflow

        out = attach_operator_workflow(enriched, proj_row, include_summary=include_summary)
    except Exception:
        out = enriched
        out.setdefault("operator_workflow_state", "DISCOVERED")
        out.setdefault("operator_blockers", list(enriched["execution_critical_blockers"]))

    # Re-assert owner-ready authority after workflow attach (workflow must never override)
    out["ready_for_owner_approval"] = bool(gate.get("ready_for_owner_approval"))
    out["owner_approval_gate"] = gate
    out["execution_critical_blockers"] = list(profile.get("execution_critical_blockers") or [])
    # Phase F — sharpen VA next action from owner-gate reasons when more specific than generic stage text
    va = list(gate.get("va_next_actions") or profile.get("va_next_actions") or [])
    if out.get("ready_for_owner_approval") is True:
        out["operator_next_action"] = "Present READY package to owner for bid approval decision."
    elif va:
        # Prefer first concrete gate reason (already plain-English from readiness)
        out["operator_next_action"] = str(va[0])
    # Ensure operator_blockers include execution-critical codes for UI plainBlockers map
    ob = list(out.get("operator_blockers") or [])
    for b in out["execution_critical_blockers"]:
        if b and b not in ob:
            ob.append(b)
    out["operator_blockers"] = ob
    out["enrichment_build"] = EXEC_BUILD
    out["enrichment_authority"] = "execution_requirements.enrichment.enrich_deal_for_operator"
    out["legacy_readiness_note"] = (
        "Legacy readiness_summary / lifecycle READY / BID_PREPARATION / checklist allPass "
        "are informational only and must not create READY FOR OWNER APPROVAL."
    )
    return out


def attach_canonical_enrichment(
    payload: dict[str, Any],
    row: dict[str, Any] | None = None,
    *,
    include_summary: bool = True,
    include_full_profile: bool = True,
) -> dict[str, Any]:
    """Merge canonical enrichment onto an existing payload (deal-room / card)."""
    out = dict(payload) if isinstance(payload, dict) else {}
    enriched = enrich_deal_for_operator(
        row if row is not None else out,
        include_summary=include_summary,
        include_full_profile=include_full_profile,
    )
    # Copy authoritative fields
    for key in (
        "execution_compliance",
        "owner_approval_gate",
        "execution_critical_blockers",
        "ready_for_owner_approval",
        "supplier_execution_state",
        "operator_workflow_state",
        "operator_state_reason",
        "operator_next_action",
        "operator_blockers",
        "confidence_level",
        "missing_information",
        "risk_flags",
        "state_conflict_detected",
        "operator_summary",
        "enrichment_build",
        "enrichment_authority",
        "legacy_readiness_note",
    ):
        if key in enriched:
            out[key] = enriched[key]
    return out


def owner_ready_banner_allowed(payload: dict[str, Any] | None) -> bool:
    """UI helper — READY banner only when gate says so."""
    if not isinstance(payload, dict):
        return False
    gate = payload.get("owner_approval_gate")
    if isinstance(gate, dict) and "ready_for_owner_approval" in gate:
        return gate.get("ready_for_owner_approval") is True
    return payload.get("ready_for_owner_approval") is True
