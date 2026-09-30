"""Full execution compliance profile for deal-room / operator projection."""

from __future__ import annotations

from typing import Any

from execution_requirements.cash_cycle import build_cash_cycle_handoff
from execution_requirements.checklists import (
    build_invoice_checklist,
    build_post_award_checklist,
    build_submission_checklist,
    build_supplier_confirmation_checklist,
    build_supplier_quote_packet,
)
from execution_requirements.conflicts import apply_amendment_overlay, detect_requirement_conflicts
from execution_requirements.constants import BUILD_TAG
from execution_requirements.extract import build_execution_requirements
from execution_requirements.readiness import build_deep_dive_sections, evaluate_owner_approval_gate


def build_execution_compliance_profile(
    row: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    amendment_text: str | None = None,
) -> dict[str, Any]:
    """
    End-to-end Phase D profile:
    requirements → conflicts → checklists → cash cycle → owner gate → deep dive sections.

    Owner readiness authority is evaluate_owner_approval_gate (tri-state safe).
    Do not pass public market price as supplier_quote_present — that path is retired.
    """
    row = row if isinstance(row, dict) else {}
    if amendment_text:
        overlay = apply_amendment_overlay(
            text or "",
            amendment_text,
            row=row,
            amendment_document="amendment",
        )
        requirements = overlay["requirements"]
        conflicts = overlay["conflicts"]
        amendment = {
            "applied": True,
            "changed_categories": overlay.get("changed_categories"),
        }
    else:
        requirements = build_execution_requirements(row, text=text)
        conflicts = detect_requirement_conflicts(requirements)
        amendment = {"applied": False}

    submission = build_submission_checklist(requirements)
    supplier = build_supplier_confirmation_checklist(requirements)
    post_award = build_post_award_checklist(
        requirements,
        lifecycle=str(row.get("lifecycle") or row.get("operator_workflow_state") or ""),
        payment_state=str(row.get("payment_state") or ""),
    )
    invoice = build_invoice_checklist(requirements)
    cash = build_cash_cycle_handoff(row, requirements=requirements)
    quote_packet = build_supplier_quote_packet(
        requirements, deal_name=str(row.get("title") or row.get("Opportunity") or row.get("canonical_id") or "Opportunity")
    )
    sections = build_deep_dive_sections(requirements)
    # Pass full row — gate resolves economics/supplier/financing with UNKNOWN-safe rules.
    # Do NOT pass legacy supplier_quote_present derived from public price evidence.
    gate = evaluate_owner_approval_gate(
        requirements=requirements,
        conflicts=conflicts,
        submission_checklist=submission,
        supplier_checklist=supplier,
        row=row,
        amendment=amendment,
    )

    return {
        "kind": "ExecutionComplianceProfile",
        "build": BUILD_TAG,
        "canonical_id": row.get("canonical_id"),
        "requirements": requirements,
        "requirement_count": len(requirements),
        "conflicts": conflicts,
        "amendment": amendment,
        "submission_checklist": submission,
        "supplier_confirmation_checklist": supplier,
        "supplier_quote_packet": quote_packet,
        "post_award_checklist": post_award,
        "invoice_checklist": invoice,
        "cash_cycle": cash,
        "deep_dive_sections": sections,
        "owner_approval_gate": gate,
        "execution_critical_blockers": list(gate.get("blockers") or []),
        "va_next_actions": list(gate.get("va_next_actions") or []),
        "ready_for_owner_approval": gate.get("ready_for_owner_approval") is True,
        "supplier_execution_state": gate.get("supplier_execution_state"),
        "legacy_readiness_fields": {
            "note": "Informational only — never authority for READY FOR OWNER APPROVAL",
            "fields": [
                "readiness_summary",
                "lifecycle READY / COMPLETE / DECISION_READY",
                "BID_PREPARATION operator state",
                "checklist allPass",
                "commercial verification worthy",
            ],
        },
    }


def attach_execution_compliance(payload: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Non-destructive attach for deal-room / API payloads.

    Prefer enrichment.enrich_deal_for_operator / attach_canonical_enrichment for
    surfaces that also need operator workflow projection.
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    profile = build_execution_compliance_profile(row if row is not None else out)
    out["execution_compliance"] = profile
    out["execution_critical_blockers"] = profile.get("execution_critical_blockers") or []
    out["ready_for_owner_approval"] = profile.get("ready_for_owner_approval") is True
    out["owner_approval_gate"] = profile.get("owner_approval_gate")
    out["supplier_execution_state"] = profile.get("supplier_execution_state")
    return out
