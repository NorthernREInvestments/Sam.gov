"""UI-facing OperatorDealSummary + dashboard prep (calculated, not persisted)."""

from __future__ import annotations

from typing import Any

from operator_workflow.constants import (
    BUILD_TAG,
    OW_AWARDED,
    OW_HISTORY,
    OW_PAID,
    OW_SUBMITTED,
)
from operator_workflow.resolver import project_operator_workflow

OPERATOR_FIELD_KEYS = (
    "operator_workflow_state",
    "operator_state_reason",
    "operator_next_action",
    "operator_blockers",
    "confidence_level",
    "missing_information",
    "risk_flags",
    "state_conflict_detected",
)

_TERMINAL_HIDE_FROM_ACTIVE = frozenset(
    {
        OW_HISTORY,
        OW_PAID,
        # Awarded stays visible as active work until ordering/paid — include AWARDED in active
    }
)


def operator_fields_dict(projection: dict[str, Any]) -> dict[str, Any]:
    """Exact Phase B contract fields for API payloads."""
    return {
        "operator_workflow_state": projection.get("operator_workflow_state") or "DISCOVERED",
        "operator_state_reason": projection.get("operator_state_reason") or "",
        "operator_next_action": projection.get("operator_next_action") or "",
        "operator_blockers": list(projection.get("operator_blockers") or []),
        "confidence_level": projection.get("confidence_level") or "UNKNOWN",
        "missing_information": list(projection.get("missing_information") or []),
        "risk_flags": list(projection.get("risk_flags") or []),
        "state_conflict_detected": bool(projection.get("state_conflict_detected")),
    }


def merge_row_for_projection(row: dict[str, Any] | None, overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a projection input from store row + optional UI card overlay (Status, Funding, etc.)."""
    merged: dict[str, Any] = {}
    if isinstance(row, dict):
        merged.update(row)
    if isinstance(overlay, dict):
        # Prefer overlay labels that the UI already shows
        if overlay.get("Status") or overlay.get("Deal_state"):
            merged["Deal_state"] = overlay.get("Deal_state") or overlay.get("Status")
            merged["Status"] = overlay.get("Status") or overlay.get("Deal_state")
        if overlay.get("Funding") is not None:
            merged["Funding"] = overlay.get("Funding")
        if overlay.get("Economics_class"):
            merged["Economics_class"] = overlay.get("Economics_class")
        if overlay.get("lifecycle"):
            merged.setdefault("lifecycle", overlay.get("lifecycle"))
        if overlay.get("canonical_id"):
            merged.setdefault("canonical_id", overlay.get("canonical_id"))
        if overlay.get("Opportunity") and not merged.get("title"):
            merged["title"] = overlay.get("Opportunity")
        if overlay.get("Agency") and not merged.get("agency"):
            merged["agency"] = overlay.get("Agency")
        if overlay.get("Deadline") and not merged.get("deadline"):
            merged["deadline"] = overlay.get("Deadline")
        if overlay.get("Known_gross_spread") is not None:
            merged.setdefault("Gross_spread", overlay.get("Known_gross_spread"))
        # Keep overlay keys available
        for k, v in overlay.items():
            if k not in merged or merged.get(k) in (None, "", "UNKNOWN"):
                merged[k] = v
    return merged


def build_operator_deal_summary(
    row: dict[str, Any] | None = None,
    *,
    overlay: dict[str, Any] | None = None,
    enrich: bool = False,
) -> dict[str, Any]:
    """
    OperatorDealSummary — answers:
    What stage? Why here? What next? What blocking? Conflicts?

    When enrich=True, runs the Phase E.1 canonical enrichment path first so
    owner readiness and execution blockers match Deal Room / Deep Dive.
    """
    merged = merge_row_for_projection(row, overlay)
    ready_flag = None
    gate = None
    blockers_exec: list[str] = []
    if enrich:
        try:
            from execution_requirements.enrichment import enrich_deal_for_operator

            enriched = enrich_deal_for_operator(merged, include_summary=False, include_full_profile=False)
            merged = enriched
            ready_flag = enriched.get("ready_for_owner_approval") is True
            gate = enriched.get("owner_approval_gate")
            blockers_exec = list(enriched.get("execution_critical_blockers") or [])
        except Exception:
            pass
    proj = project_operator_workflow(merged)
    fields = operator_fields_dict(proj)
    value: Any = merged.get("estimated_value") or merged.get("estimated_opportunity_size")
    if value in (None, ""):
        te = merged.get("transaction_economics")
        if isinstance(te, dict):
            value = te.get("revenue")
    if value in (None, ""):
        value = merged.get("government_revenue") or merged.get("supported_revenue") or "UNKNOWN"
    if ready_flag is None:
        ready_flag = merged.get("ready_for_owner_approval") is True
    if gate is None:
        gate = merged.get("owner_approval_gate")
    return {
        "kind": "OperatorDealSummary",
        "build": BUILD_TAG,
        "canonical_id": merged.get("canonical_id"),
        "deal_name": merged.get("title") or merged.get("Opportunity") or "Untitled",
        "agency": merged.get("agency") or merged.get("Agency") or "UNKNOWN",
        "deadline": merged.get("deadline") or merged.get("Deadline") or "UNKNOWN",
        "estimated_value": value if value not in (None, "") else "UNKNOWN",
        **fields,
        "conflicting_states": list(proj.get("conflicting_states") or []),
        # Explicit Q&A shape for Phase C UI
        "stage": fields["operator_workflow_state"],
        "why_here": fields["operator_state_reason"],
        "what_next": fields["operator_next_action"],
        "blocking_progress": fields["operator_blockers"],
        "has_contradiction": fields["state_conflict_detected"],
        # Phase E.1 — owner readiness (never derived from BID_PREPARATION alone)
        "ready_for_owner_approval": ready_flag is True,
        "owner_approval_gate": gate,
        "execution_critical_blockers": blockers_exec or list(merged.get("execution_critical_blockers") or []),
        "supplier_execution_state": merged.get("supplier_execution_state"),
    }


def attach_operator_workflow(
    payload: dict[str, Any],
    row: dict[str, Any] | None = None,
    *,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Non-destructive: copy payload and add operator workflow fields (calculated)."""
    out = dict(payload) if isinstance(payload, dict) else {}
    summary = build_operator_deal_summary(row if row is not None else out, overlay=out)
    fields = operator_fields_dict(summary)
    out.update(fields)
    # Preserve owner-ready if already set by enrichment; never invent from workflow state
    if "ready_for_owner_approval" in out and out.get("owner_approval_gate"):
        out["ready_for_owner_approval"] = (
            (out.get("owner_approval_gate") or {}).get("ready_for_owner_approval") is True
        )
    elif summary.get("ready_for_owner_approval") is True:
        out["ready_for_owner_approval"] = True
    if include_summary:
        out["operator_summary"] = summary
    return out


def _attention_category(summary: dict[str, Any]) -> str | None:
    blockers = set(summary.get("operator_blockers") or [])
    risks = set(summary.get("risk_flags") or [])
    exec_b = set(summary.get("execution_critical_blockers") or [])
    if summary.get("state_conflict_detected"):
        return "state_conflict"
    if "SUPPLIER_UNKNOWN" in blockers or "SUPPLIER_QUOTE_NEEDED" in risks or "SUPPLIER_NOT_VALIDATED" in exec_b:
        return "missing_supplier_quote"
    if "ECONOMICS_UNKNOWN" in blockers or "ECONOMICS_INCOMPLETE_OR_UNKNOWN" in exec_b:
        return "unknown_economics"
    if "FUNDING_UNRESOLVED" in blockers or "FINANCING_INCOMPATIBLE_OR_UNKNOWN" in exec_b:
        return "financing_issue"
    if summary.get("missing_information"):
        return "missing_information"
    return None


def build_operator_dashboard_payload(rows: list[dict[str, Any]], *, enrich: bool = False) -> dict[str, Any]:
    """
    Backend preparation for Phase C Dashboard.
    Active Work + Attention Needed.

    When rows were already passed through enrich_deal_for_operator, keep enrich=False
    (default) so readiness matches Deal Room without double work.
    Pass enrich=True only when callers have raw store rows and need gate truth.
    """
    summaries = [build_operator_deal_summary(r, enrich=enrich) for r in rows if isinstance(r, dict)]
    active_work = []
    for s in summaries:
        st = s.get("operator_workflow_state")
        if st in _TERMINAL_HIDE_FROM_ACTIVE:
            continue
        active_work.append(
            {
                "canonical_id": s.get("canonical_id"),
                "deal_name": s.get("deal_name"),
                "operator_workflow_state": st,
                "operator_next_action": s.get("operator_next_action"),
                "deadline": s.get("deadline"),
                "estimated_value": s.get("estimated_value"),
                "operator_blockers": s.get("operator_blockers") or [],
                "execution_critical_blockers": s.get("execution_critical_blockers") or [],
                "state_conflict_detected": bool(s.get("state_conflict_detected")),
                "confidence_level": s.get("confidence_level"),
                "ready_for_owner_approval": s.get("ready_for_owner_approval") is True,
                "owner_approval_gate": s.get("owner_approval_gate"),
                "supplier_execution_state": s.get("supplier_execution_state"),
            }
        )

    attention: dict[str, list[dict[str, Any]]] = {
        "missing_supplier_quote": [],
        "unknown_economics": [],
        "financing_issue": [],
        "state_conflict": [],
        "missing_information": [],
    }
    for s in summaries:
        cat = _attention_category(s)
        if not cat:
            continue
        attention.setdefault(cat, []).append(
            {
                "canonical_id": s.get("canonical_id"),
                "deal_name": s.get("deal_name"),
                "operator_workflow_state": s.get("operator_workflow_state"),
                "operator_next_action": s.get("operator_next_action"),
                "operator_blockers": s.get("operator_blockers") or [],
                "execution_critical_blockers": s.get("execution_critical_blockers") or [],
                "risk_flags": s.get("risk_flags") or [],
                "state_conflict_detected": bool(s.get("state_conflict_detected")),
                "ready_for_owner_approval": s.get("ready_for_owner_approval") is True,
            }
        )

    return {
        "kind": "OperatorDashboardPrep",
        "build": BUILD_TAG,
        "active_work": active_work[:40],
        "attention_needed": attention,
        "counts": {
            "active_work": len(active_work),
            "attention_total": sum(len(v) for v in attention.values()),
            "state_conflicts": len(attention.get("state_conflict") or []),
            "unknown_economics": len(attention.get("unknown_economics") or []),
            "missing_supplier_quote": len(attention.get("missing_supplier_quote") or []),
            "financing_issue": len(attention.get("financing_issue") or []),
            "owner_ready": sum(1 for s in active_work if s.get("ready_for_owner_approval") is True),
        },
        "note": (
            "Calculated projection via enrich_deal_for_operator. "
            "READY FOR OWNER APPROVAL only when owner_approval_gate.ready_for_owner_approval is true."
        ),
    }
