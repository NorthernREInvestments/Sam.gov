"""Amendment/Q&A material change detection + dependency-aware invalidation."""

from __future__ import annotations

import re
from typing import Any

from bid_compliance_constants import (
    CHG_AUTHORIZATION,
    CHG_CANCEL,
    CHG_DEADLINE,
    CHG_DELIVERY,
    CHG_DOMESTIC,
    CHG_ELIGIBILITY,
    CHG_EVALUATION,
    CHG_FORM,
    CHG_OTHER,
    CHG_PRODUCT,
    CHG_QA,
    CHG_QUANTITY,
    CHG_SPEC,
    CHG_SUBMISSION,
    TRACKED_CHANGE_ACK_REQUIRED,
)

# Align with tracked_solicitation.DEPENDENCY_MAP concepts
COMPLIANCE_DEPENDENCY_MAP: dict[str, list[str]] = {
    CHG_QUANTITY: [
        "bom_quantity",
        "supplier_economics",
        "freight",
        "financing_requirement",
        "profit",
        "bid_price_assumptions",
    ],
    CHG_SPEC: [
        "product_compliance",
        "supplier_match",
        "historical_comparables",
        "economics",
    ],
    CHG_PRODUCT: [
        "product_compliance",
        "supplier_match",
        "economics",
    ],
    CHG_DEADLINE: ["deadline_viability", "priority", "bid_readiness"],
    CHG_DELIVERY: [
        "supplier_lead_time",
        "freight_mode",
        "delivery_viability",
        "economics",
    ],
    CHG_AUTHORIZATION: ["product_compliance", "channel_compliance"],
    CHG_DOMESTIC: ["origin_compliance", "product_compliance"],
    CHG_ELIGIBILITY: ["bidder_eligibility", "pursuit_qualification"],
    CHG_FORM: ["bid_form_readiness", "compliance_matrix"],
    CHG_SUBMISSION: ["submission_instructions", "bid_readiness"],
    CHG_EVALUATION: ["pursuit_intelligence"],
    CHG_QA: ["affected_requirement_reevaluation"],
    CHG_CANCEL: ["pursuit_qualification", "bid_readiness"],
    CHG_OTHER: ["compliance_matrix"],
}


def classify_material_change(
    *,
    old_text: str | None = None,
    new_text: str | None = None,
    change_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hints = change_hints or {}
    categories: list[str] = []
    if hints.get("quantity_old") is not None and hints.get("quantity_new") is not None:
        if hints["quantity_old"] != hints["quantity_new"]:
            categories.append(CHG_QUANTITY)
    if hints.get("cancelled"):
        categories.append(CHG_CANCEL)
    if hints.get("deadline_changed"):
        categories.append(CHG_DEADLINE)

    new_l = (new_text or "").lower()
    old_l = (old_text or "").lower()
    if "authorization" in new_l and "authorization" not in old_l:
        categories.append(CHG_AUTHORIZATION)
    if re.search(r"\bbuy\s+american\b|\btaa\b|\bdomestic\b", new_l) and not re.search(
        r"\bbuy\s+american\b|\btaa\b|\bdomestic\b", old_l
    ):
        categories.append(CHG_DOMESTIC)
    if hints.get("is_qa"):
        categories.append(CHG_QA)
    if hints.get("spec_changed") or (
        "specification" in new_l and hints.get("quantity_old") is None
    ):
        if CHG_SPEC not in categories and hints.get("spec_changed"):
            categories.append(CHG_SPEC)
    if hints.get("product_changed"):
        categories.append(CHG_PRODUCT)
    if hints.get("delivery_changed"):
        categories.append(CHG_DELIVERY)
    if hints.get("form_changed"):
        categories.append(CHG_FORM)
    if hints.get("submission_changed"):
        categories.append(CHG_SUBMISSION)
    if hints.get("evaluation_changed"):
        categories.append(CHG_EVALUATION)
    if hints.get("eligibility_changed"):
        categories.append(CHG_ELIGIBILITY)

    if not categories:
        categories.append(CHG_OTHER)

    affected: list[str] = []
    for cat in categories:
        for dep in COMPLIANCE_DEPENDENCY_MAP.get(cat, []):
            if dep not in affected:
                affected.append(dep)

    return {
        "kind": "MaterialChangeClassification",
        "categories": categories,
        "affected_dependencies": affected,
        "unaffected_preserved": True,
        "operator_acknowledgment": TRACKED_CHANGE_ACK_REQUIRED,
        "material": any(c != CHG_OTHER for c in categories) or bool(hints.get("force_material")),
    }


def apply_invalidation(
    research_state: dict[str, Any],
    *,
    change: dict[str, Any],
) -> dict[str, Any]:
    """Invalidate only affected keys; leave unrelated research intact."""
    state = dict(research_state or {})
    intact = dict(state.get("conclusions") or {})
    invalidated = []
    for dep in change.get("affected_dependencies") or []:
        if dep in intact:
            intact[dep] = {
                "status": "INVALIDATED",
                "reason": f"material_change:{','.join(change.get('categories') or [])}",
                "prior": intact[dep],
            }
            invalidated.append(dep)
        else:
            intact[dep] = {"status": "INVALIDATED", "reason": "dependency_flagged"}
            invalidated.append(dep)
    # Preserve keys not in affected list
    preserved = [k for k in (research_state.get("conclusions") or {}) if k not in (change.get("affected_dependencies") or [])]
    state["conclusions"] = intact
    state["last_invalidation"] = {
        "categories": change.get("categories"),
        "invalidated": invalidated,
        "preserved": preserved,
    }
    state["operator_acknowledgment_required"] = True
    return state


def process_amendment_or_qa(
    *,
    package_map: dict[str, Any],
    amendment_doc: dict[str, Any],
    old_requirement_values: dict[str, Any] | None = None,
    new_requirement_values: dict[str, Any] | None = None,
    research_state: dict[str, Any] | None = None,
    is_qa: bool = False,
) -> dict[str, Any]:
    from governing_documents import apply_amendment_supersession, resolve_requirement_authority

    old_vals = old_requirement_values or {}
    new_vals = new_requirement_values or {}
    hints: dict[str, Any] = {"is_qa": is_qa}
    if "quantity" in old_vals or "quantity" in new_vals:
        hints["quantity_old"] = old_vals.get("quantity")
        hints["quantity_new"] = new_vals.get("quantity")
    if "deadline" in old_vals and "deadline" in new_vals and old_vals["deadline"] != new_vals["deadline"]:
        hints["deadline_changed"] = True
    if "delivery_days" in old_vals and "delivery_days" in new_vals:
        if old_vals["delivery_days"] != new_vals["delivery_days"]:
            hints["delivery_changed"] = True
    if "spec" in new_vals and new_vals.get("spec") != old_vals.get("spec"):
        hints["spec_changed"] = True

    change = classify_material_change(
        old_text=str(old_vals),
        new_text=str(new_vals) + " " + str(amendment_doc.get("text_snippet") or ""),
        change_hints=hints,
    )
    new_map = apply_amendment_supersession(
        package_map,
        amendment_doc=amendment_doc,
        supersedes_document_id=amendment_doc.get("supersedes"),
    )

    authority_resolutions = {}
    for field in set(old_vals) | set(new_vals):
        candidates = []
        if field in old_vals:
            candidates.append(
                {
                    "value": old_vals[field],
                    "document_id": "prior",
                    "amendment_number": 0,
                    "authority": "GOVERNING",
                }
            )
        if field in new_vals:
            candidates.append(
                {
                    "value": new_vals[field],
                    "document_id": amendment_doc.get("document_id"),
                    "amendment_number": amendment_doc.get("amendment_number") or 1,
                    "authority": "GOVERNING",
                }
            )
        authority_resolutions[field] = resolve_requirement_authority(field=field, candidates=candidates)

    updated_research = apply_invalidation(research_state or {"conclusions": {}}, change=change)
    return {
        "kind": "AmendmentQAProcessing",
        "package_map": new_map,
        "material_change": change,
        "authority_resolutions": authority_resolutions,
        "research_state": updated_research,
        "compliance_rerun_required": True,
        "full_unrelated_research_rerun": False,
    }
