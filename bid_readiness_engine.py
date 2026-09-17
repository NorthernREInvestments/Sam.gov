"""Package completeness + bid readiness ladder + commercial verification gate.

Independent of profit/economics dimension.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from authoritative_freshness_gate import evaluate_authoritative_freshness_gate
from bid_compliance_constants import (
    FUTURE_ACTION,
    PKG_BID_ASSEMBLY,
    PKG_BLOCKED,
    PKG_COMPLIANCE,
    PKG_MATERIAL_MISSING,
    PKG_OPERATOR_VERIFY,
    PKG_PARTIAL,
    PKG_REQ_EXTRACTED,
    PKG_UNKNOWN,
    READY_AUTO_RESEARCH,
    READY_BID_ASSEMBLY,
    READY_BLOCKED,
    READY_COMMERCIAL,
    READY_COMPLIANCE,
    READY_DISCOVERED,
    READY_GAPS,
    READY_OPERATOR,
    READY_PACKAGE_ACQUIRED,
    READY_PACKAGE_MAPPED,
    READY_REQ_EXTRACTED,
    READY_TOO_LATE,
    ST_BLOCKED,
    UI_BADGE_ACK,
    UI_BADGE_BLOCKED,
    UI_BADGE_FUTURE,
    UI_BADGE_READY,
    UI_BADGE_UNRESOLVED,
)


def evaluate_package_completeness(
    package_map: dict[str, Any] | None,
    *,
    requirements_extracted: bool = False,
    compliance_analyzed: bool = False,
    material_docs_missing: bool = False,
    hard_conflicts: bool = False,
    mandatory_forms_missing: bool = False,
    eligibility_unresolved: bool = False,
    freshness_ok: bool = True,
    unacked_material_changes: bool = False,
    deadline_too_late: bool = False,
) -> dict[str, Any]:
    if not package_map or not package_map.get("documents"):
        return {"status": PKG_UNKNOWN, "ready_for_bid_assembly": False, "reasons": ["no_package"]}

    if deadline_too_late:
        return {"status": PKG_BLOCKED, "ready_for_bid_assembly": False, "reasons": ["TOO_LATE"]}
    if hard_conflicts or unacked_material_changes or not freshness_ok:
        reasons = []
        if hard_conflicts:
            reasons.append("unresolved_hard_conflicts")
        if unacked_material_changes:
            reasons.append("unacknowledged_material_changes")
        if not freshness_ok:
            reasons.append("authoritative_freshness_failed")
        return {"status": PKG_BLOCKED, "ready_for_bid_assembly": False, "reasons": reasons}
    if material_docs_missing:
        return {"status": PKG_MATERIAL_MISSING, "ready_for_bid_assembly": False, "reasons": ["material_documents_missing"]}
    if mandatory_forms_missing or eligibility_unresolved:
        return {
            "status": PKG_OPERATOR_VERIFY,
            "ready_for_bid_assembly": False,
            "reasons": [
                x
                for x in [
                    "mandatory_forms_missing" if mandatory_forms_missing else None,
                    "eligibility_unresolved" if eligibility_unresolved else None,
                ]
                if x
            ],
        }
    if compliance_analyzed and requirements_extracted:
        # Still need operator verification before assembly unless everything clear
        return {
            "status": PKG_OPERATOR_VERIFY,
            "ready_for_bid_assembly": False,
            "reasons": ["operator_verification_required"],
            "note": "PACKAGE_READY_FOR_BID_ASSEMBLY only when gaps cleared",
        }
    if requirements_extracted:
        return {"status": PKG_REQ_EXTRACTED, "ready_for_bid_assembly": False, "reasons": []}
    if package_map.get("governing_count", 0) > 0:
        return {"status": PKG_PARTIAL, "ready_for_bid_assembly": False, "reasons": ["requirements_not_extracted"]}
    return {"status": PKG_PARTIAL, "ready_for_bid_assembly": False, "reasons": ["incomplete_map"]}


def advance_to_bid_assembly_eligible(completeness: dict[str, Any], *, gaps_cleared: bool) -> dict[str, Any]:
    out = dict(completeness)
    if gaps_cleared and completeness.get("status") in {PKG_OPERATOR_VERIFY, PKG_COMPLIANCE}:
        out["status"] = PKG_BID_ASSEMBLY
        out["ready_for_bid_assembly"] = True
        out["reasons"] = []
    return out


def evaluate_bid_readiness_ladder(
    *,
    discovered: bool = True,
    package_acquired: bool = False,
    package_mapped: bool = False,
    requirements_extracted: bool = False,
    compliance_analyzed: bool = False,
    material_gaps: bool = False,
    automated_research_complete: bool = False,
    commercial_verification_needed: bool = True,
    operator_verification_needed: bool = True,
    blockers: list[str] | None = None,
    deadline_viability: str | None = None,
    freshness_gate: dict[str, Any] | None = None,
    compliance_matrix: dict[str, Any] | None = None,
    package_completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Readiness independent of profit. Never READY_FOR_BID_ASSEMBLY with hard gaps."""
    blockers = list(blockers or [])
    matrix = compliance_matrix or {}
    if matrix.get("hard_blocker_present") or matrix.get("counts", {}).get("blockers", 0) > 0:
        blockers.append("compliance_hard_blocker")
    if matrix.get("unresolved_mandatory"):
        blockers.append("unresolved_mandatory_requirements")
    fg = freshness_gate or {}
    if fg and not (fg.get("passed") or fg.get("would_allow_ready_for_submission")) and fg.get("blockers"):
        blockers.append("authoritative_freshness_blocked")
        if "unreviewed_material_or_critical_changes" in (fg.get("blockers") or []):
            blockers.append("tracked_stale_or_unreviewed_change")
    if deadline_viability == "TOO_LATE":
        return {
            "kind": "BidReadiness",
            "state": READY_TOO_LATE,
            "blockers": ["TOO_LATE"],
            "ready_for_bid_assembly": False,
            "badge": UI_BADGE_BLOCKED,
            "independent_of_profit": True,
        }
    if blockers and any("hard" in b or "blocker" in b or "freshness" in b or "TOO_LATE" in b for b in blockers):
        state = READY_BLOCKED
        badge = UI_BADGE_BLOCKED
    elif not discovered:
        state = READY_DISCOVERED
        badge = UI_BADGE_UNRESOLVED
    elif not package_acquired:
        state = READY_DISCOVERED
        badge = UI_BADGE_UNRESOLVED
    elif not package_mapped:
        state = READY_PACKAGE_ACQUIRED
        badge = UI_BADGE_UNRESOLVED
    elif not requirements_extracted:
        state = READY_PACKAGE_MAPPED
        badge = UI_BADGE_UNRESOLVED
    elif not compliance_analyzed:
        state = READY_REQ_EXTRACTED
        badge = UI_BADGE_UNRESOLVED
    elif material_gaps or matrix.get("unresolved_mandatory"):
        state = READY_GAPS
        badge = UI_BADGE_UNRESOLVED
    elif not automated_research_complete:
        state = READY_COMPLIANCE
        badge = UI_BADGE_UNRESOLVED
    elif commercial_verification_needed:
        state = READY_COMMERCIAL
        badge = UI_BADGE_FUTURE
    elif operator_verification_needed:
        state = READY_OPERATOR
        badge = UI_BADGE_ACK
    elif package_completeness and package_completeness.get("ready_for_bid_assembly"):
        state = READY_BID_ASSEMBLY
        badge = UI_BADGE_READY
    else:
        state = READY_OPERATOR
        badge = UI_BADGE_ACK

    # Explicit gate: unresolved mandatory prevents assembly
    if matrix.get("unresolved_mandatory") or matrix.get("hard_blocker_present"):
        if state == READY_BID_ASSEMBLY:
            state = READY_BLOCKED
            badge = UI_BADGE_BLOCKED
        blockers = list(dict.fromkeys(blockers + ["unresolved_mandatory_prevents_READY_FOR_BID_ASSEMBLY"]))

    return {
        "kind": "BidReadiness",
        "state": state,
        "blockers": list(dict.fromkeys(blockers)),
        "ready_for_bid_assembly": state == READY_BID_ASSEMBLY,
        "badge": badge,
        "independent_of_profit": True,
        "package_completeness": (package_completeness or {}).get("status"),
        "evaluated_at": now_utc().isoformat(),
    }


def commercial_verification_gate(
    *,
    pursuit_worthy: bool = False,
    package_materially_complete: bool = False,
    compliance_achievable: bool = False,
    economics_potentially_viable: bool = False,
    deadline_actionable: bool = False,
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    """Handoff plan only — never performs outreach."""
    eligible = all(
        [
            pursuit_worthy,
            package_materially_complete,
            compliance_achievable,
            economics_potentially_viable,
            deadline_actionable,
        ]
    )
    actions = [
        {"action": "obtain_binding_supplier_quote", "status": FUTURE_ACTION},
        {"action": "verify_stock", "status": FUTURE_ACTION},
        {"action": "verify_lead_time", "status": FUTURE_ACTION},
        {"action": "verify_authorized_channel", "status": FUTURE_ACTION},
        {"action": "obtain_manufacturer_authorization", "status": FUTURE_ACTION},
        {"action": "obtain_financing_indication", "status": FUTURE_ACTION},
        {"action": "confirm_freight", "status": FUTURE_ACTION},
        {"action": "verify_registration", "status": FUTURE_ACTION},
        {"action": "obtain_insurance_certificate", "status": FUTURE_ACTION},
    ]
    # Filter to relevant gaps if provided
    if gaps:
        filtered = []
        for a in actions:
            key = a["action"]
            if any(g.replace(" ", "_") in key or key in g for g in gaps):
                filtered.append(a)
        if filtered:
            actions = filtered
    return {
        "kind": "CommercialVerificationGate",
        "eligible_for_plan": eligible,
        "actions": actions if eligible else [],
        "performed": False,
        "outreach_count": 0,
        "note": "FUTURE_ACTION_IF_PURSUED only — DEVELOPMENT_NO_OUTREACH",
        "gaps": gaps or [],
    }


def build_operator_checklist(
    *,
    solicitation_id: str,
    readiness: dict[str, Any],
    package_map: dict[str, Any],
    compliance_matrix: dict[str, Any],
    registrations: dict[str, Any],
    forms: dict[str, Any],
    submission: dict[str, Any],
    product_compliance: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    commercial: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every item traceable to source — no generic boilerplate."""
    sections = []

    def item(label: str, status: str, evidence: Any, source: str | None = None) -> dict[str, Any]:
        return {"label": label, "status": status, "evidence": evidence, "source": source}

    sections.append(
        {
            "section": "Opportunity",
            "items": [item("Solicitation ID", "INFO", solicitation_id, "package_map")],
        }
    )
    sections.append(
        {
            "section": "Deadline",
            "items": [
                item("Bid deadline", submission.get("deadline") or "UNKNOWN", submission.get("deadline"), "submission_instructions"),
                item("Timezone", "KNOWN" if submission.get("timezone_known") else "UNKNOWN", submission.get("timezone"), "submission_instructions"),
            ],
        }
    )
    sections.append(
        {
            "section": "Package completeness",
            "items": [
                item("Governing docs", str(package_map.get("governing_count")), package_map.get("governing_documents"), "package_map"),
                item("Readiness", readiness.get("state"), readiness.get("blockers"), "bid_readiness"),
            ],
        }
    )
    matrix_items = [
        item(r.get("requirement"), r.get("current_status"), r.get("source_snippet"), r.get("source"))
        for r in (compliance_matrix.get("rows") or [])
        if r.get("mandatory")
    ]
    sections.append({"section": "Mandatory requirements", "items": matrix_items[:40]})
    sections.append(
        {
            "section": "Registrations",
            "items": [
                item(i.get("portal"), i.get("registration_status"), i.get("requirement"), "registration_requirements")
                for i in (registrations.get("items") or [])
            ],
        }
    )
    sections.append(
        {
            "section": "Required forms",
            "items": [
                item(f.get("form_key"), f.get("requirement"), f.get("operator_action"), "required_forms")
                for f in (forms.get("forms") or [])
            ],
        }
    )
    sections.append(
        {
            "section": "Submission instructions",
            "items": [
                item("Method", submission.get("submission_method") or "UNKNOWN", submission.get("portal") or submission.get("email"), "submission_instructions"),
            ],
        }
    )
    if product_compliance:
        sections.append(
            {
                "section": "Product compliance",
                "items": [item("State", product_compliance.get("state"), product_compliance.get("substitution_policy"), "product_compliance")],
            }
        )
    if evaluation:
        sections.append(
            {
                "section": "Evaluation",
                "items": [item("Award basis", evaluation.get("award_basis") or "UNKNOWN", evaluation.get("factors"), "evaluation_basis")],
            }
        )
    if commercial:
        sections.append(
            {
                "section": "Commercial verification",
                "items": [
                    item(a["action"], a["status"], FUTURE_ACTION, "commercial_verification_gate")
                    for a in (commercial.get("actions") or [])
                ],
            }
        )
    sections.append(
        {
            "section": "Remaining blockers",
            "items": [item(b, "BLOCKED", b, "bid_readiness") for b in (readiness.get("blockers") or [])],
        }
    )
    if freshness:
        sections.append(
            {
                "section": "Authoritative freshness",
                "items": [item("Freshness", "PASS" if (freshness.get("passed") or freshness.get("would_allow_ready_for_submission")) else "FAIL", freshness.get("blockers"), "freshness_gate")],
            }
        )
    return {
        "kind": "BidChecklist",
        "solicitation_id": solicitation_id,
        "sections": sections,
        "boilerplate_avoided": True,
        "traceable_to_evidence": True,
    }


def freshness_for_readiness(
    *,
    last_check_at: str | None,
    current_version_confirmed: bool = True,
    amendment_set_confirmed: bool = True,
    unreviewed_material_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return evaluate_authoritative_freshness_gate(
        last_authoritative_check_at=last_check_at,
        current_version_confirmed=current_version_confirmed,
        amendment_set_confirmed=amendment_set_confirmed,
        unreviewed_material_changes=unreviewed_material_changes,
    )
