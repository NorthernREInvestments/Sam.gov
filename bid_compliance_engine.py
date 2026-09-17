"""Bid requirements + compliance intelligence orchestrator.

Reuses package map, requirement extraction, compliance matrix, product/submission
intelligence, readiness ladder, invalidation, and Cost Governor.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from bid_compliance_constants import FUTURE_ACTION
from bid_compliance_invalidation import process_amendment_or_qa
from bid_readiness_engine import (
    build_operator_checklist,
    commercial_verification_gate,
    evaluate_bid_readiness_ladder,
    evaluate_package_completeness,
    freshness_for_readiness,
)
from bid_requirement_extraction import (
    distinguish_mandatory_vs_informational,
    extract_requirements_from_package,
)
from bid_submission_intelligence import (
    delivery_invalidates_economics,
    extract_delivery_logistics,
    extract_evaluation_basis,
    extract_registration_requirements,
    extract_required_forms,
    extract_submission_instructions,
)
from compliance_matrix import build_compliance_matrix
from cost_governor import get_cost_governor, research_value_decision
from cost_governor_constants import TIER_1_ACTIVE, TIER_2_QUALIFIED
from governing_documents import build_package_map, governing_document
from operating_mode import get_operating_mode, mode_snapshot
from product_bid_compliance import (
    classify_authorization_requirement,
    classify_origin_compliance,
    evaluate_product_compliance,
    extract_brand_or_equal_details,
)


def _coerce_governing(d: dict[str, Any]) -> bool | None:
    if "governing" in d and isinstance(d.get("governing"), bool):
        return d["governing"]
    auth = d.get("authority")
    if auth is True:
        return True
    if isinstance(auth, str) and auth.upper() in {"GOVERNING", "TRUE", "YES"}:
        return True
    if isinstance(auth, str) and auth.upper() in {"REFERENCE_ONLY", "NON_GOVERNING", "FALSE", "NO"}:
        return False
    return None


def _combined_text(document_texts: dict[str, str] | None, package_map: dict[str, Any]) -> str:
    parts = []
    texts = document_texts or {}
    for doc in package_map.get("documents") or []:
        did = doc.get("document_id")
        body = texts.get(did) or texts.get(doc.get("filename") or "") or doc.get("text_snippet") or ""
        if body:
            parts.append(str(body))
    return "\n\n".join(parts)


def authorize_compliance_research(
    *,
    solicitation_id: str,
    question: str,
    could_change_readiness: bool = True,
    estimated_max_cost: float = 0.15,
    priority_tier: str | None = None,
    reusable_evidence_sufficient: bool = False,
    already_researched_fresh: bool = False,
    governor: Any = None,
) -> dict[str, Any]:
    """All paid compliance research goes through Cost Governor + VOI."""
    voi = research_value_decision(
        question=question,
        could_change_pursuit_or_readiness=could_change_readiness,
        reusable_evidence_sufficient=reusable_evidence_sufficient,
        already_researched_fresh=already_researched_fresh,
    )
    if not voi.get("allow_paid"):
        return {
            "authorized": False,
            "cost_status": "DEFERRED_VALUE",
            "reason": voi.get("reason"),
            "value": voi,
            "paid_research": False,
        }
    gov = governor or get_cost_governor()
    return gov.authorize(
        {
            "provider": "sim",
            "action_type": "AI_COMPLETION",
            "deal_id": solicitation_id,
            "priority_tier": priority_tier or TIER_2_QUALIFIED,
            "tracked": priority_tier == TIER_1_ACTIVE,
            "question": question,
            "could_change_decision": could_change_readiness,
            "estimated_max_cost": estimated_max_cost,
            "idempotency_key": f"compliance:{solicitation_id}:{question[:60]}",
            "reusable_evidence_sufficient": reusable_evidence_sufficient,
            "already_researched_fresh": already_researched_fresh,
        }
    )


def analyze_bid_compliance(
    *,
    solicitation_id: str,
    documents: list[dict[str, Any]] | None = None,
    document_texts: dict[str, str] | None = None,
    company_profile: dict[str, Any] | None = None,
    company_facts: dict[str, Any] | None = None,
    product: dict[str, Any] | None = None,
    deadline_viability: str | None = None,
    freshness: dict[str, Any] | None = None,
    pursuit_worthy: bool = False,
    economics_potentially_viable: bool = False,
    assumed_lead_time_days: int | None = None,
    known_registrations: dict[str, Any] | None = None,
    research_state: dict[str, Any] | None = None,
    unreviewed_material_changes: list[dict[str, Any]] | None = None,
    last_authoritative_check_at: str | None = None,
    paid_research_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Full compliance intelligence pass — no outreach, simulated/paid via governor only."""
    mode = get_operating_mode()
    docs_in = []
    for d in documents or []:
        if d.get("kind") == "GoverningDocument":
            docs_in.append(d)
        else:
            docs_in.append(
                governing_document(
                    solicitation_id=solicitation_id,
                    filename=d.get("filename") or d.get("document_name"),
                    document_type=d.get("document_type") or "UNKNOWN",
                    source_url=d.get("source_url") or d.get("url"),
                    authoritative_source=d.get("authoritative_source") or d.get("source"),
                    publication_date=d.get("publication_date"),
                    revision=d.get("revision") or d.get("version"),
                    amendment_number=d.get("amendment_number"),
                    supersedes=d.get("supersedes"),
                    superseded_by=d.get("superseded_by"),
                    governing=_coerce_governing(d),
                    checksum=d.get("checksum") or d.get("sha256"),
                    extracted_text=d.get("extracted_text") or d.get("text"),
                    text_available=bool(d.get("extracted_text") or d.get("text") or d.get("text_snippet")),
                    provenance=d.get("provenance"),
                    confidence=d.get("confidence") or "UNKNOWN",
                    document_id=d.get("document_id"),
                )
            )

    package_map = build_package_map(solicitation_id=solicitation_id, documents=docs_in)
    requirements = extract_requirements_from_package(package_map, document_texts=document_texts)
    mand_info = distinguish_mandatory_vs_informational(requirements)
    combined = _combined_text(document_texts, package_map)

    brand = extract_brand_or_equal_details(combined)
    authz = classify_authorization_requirement(combined)
    origin = classify_origin_compliance(combined, product_origin=(product or {}).get("origin"))
    prod = evaluate_product_compliance(
        required_manufacturer=(product or {}).get("required_manufacturer"),
        required_model=(product or {}).get("required_model"),
        required_part=(product or {}).get("required_part"),
        offered_manufacturer=(product or {}).get("offered_manufacturer"),
        offered_model=(product or {}).get("offered_model"),
        offered_part=(product or {}).get("offered_part"),
        substitution_policy=brand,
        authorization=authz,
        origin=origin,
        package_text=combined,
    )
    registrations = extract_registration_requirements(combined, known_registrations=known_registrations)
    forms = extract_required_forms(combined)
    submission = extract_submission_instructions(combined)
    evaluation = extract_evaluation_basis(combined)
    delivery = extract_delivery_logistics(combined)
    delivery_econ = delivery_invalidates_economics(
        assumed_lead_time_days=assumed_lead_time_days,
        required_delivery_days=delivery.get("delivery_window_days"),
    )

    product_states = {
        "BRAND_OR_EQUAL": {"state": prod.get("state")},
        "EXACT_BRAND_REQUIRED": {"state": prod.get("state")},
        "AUTHORIZED_RESELLER": {"state": authz.get("state")},
        "COUNTRY_OF_ORIGIN": {"state": origin.get("state")},
    }
    matrix = build_compliance_matrix(
        requirements,
        company_profile=company_profile,
        company_facts=company_facts,
        product_states=product_states,
    )

    fg = freshness or freshness_for_readiness(
        last_check_at=last_authoritative_check_at or now_utc().isoformat(),
        unreviewed_material_changes=unreviewed_material_changes,
    )
    pkg_complete = evaluate_package_completeness(
        package_map,
        requirements_extracted=bool(requirements),
        compliance_analyzed=True,
        material_docs_missing=package_map.get("governing_count", 0) == 0,
        hard_conflicts=bool(matrix.get("hard_blocker_present")),
        mandatory_forms_missing=any(f.get("form_status") == "FORM_MISSING" for f in (forms.get("forms") or []) if f.get("requirement") == "REQUIRED"),
        eligibility_unresolved=any(
            r.get("category") == "BIDDER_QUALIFICATION" and r.get("current_status") in {"UNRESOLVED", "BLOCKED", "UNSATISFIED"}
            for r in matrix.get("rows") or []
        ),
        freshness_ok=bool(fg.get("passed") or fg.get("would_allow_ready_for_submission")),
        unacked_material_changes=bool(unreviewed_material_changes),
        deadline_too_late=deadline_viability == "TOO_LATE",
    )

    readiness = evaluate_bid_readiness_ladder(
        discovered=True,
        package_acquired=bool(package_map.get("documents")),
        package_mapped=True,
        requirements_extracted=bool(requirements),
        compliance_analyzed=True,
        material_gaps=bool(matrix.get("unresolved_mandatory")),
        automated_research_complete=True,
        commercial_verification_needed=True,
        operator_verification_needed=True,
        deadline_viability=deadline_viability,
        freshness_gate=fg,
        compliance_matrix=matrix,
        package_completeness=pkg_complete,
    )

    commercial = commercial_verification_gate(
        pursuit_worthy=pursuit_worthy,
        package_materially_complete=package_map.get("governing_count", 0) > 0,
        compliance_achievable=not matrix.get("hard_blocker_present"),
        economics_potentially_viable=economics_potentially_viable,
        deadline_actionable=deadline_viability not in {"TOO_LATE", "EXPIRED"},
        gaps=[r.get("category") for r in (matrix.get("future_actions") or [])],
    )

    checklist = build_operator_checklist(
        solicitation_id=solicitation_id,
        readiness=readiness,
        package_map=package_map,
        compliance_matrix=matrix,
        registrations=registrations,
        forms=forms,
        submission=submission,
        product_compliance=prod,
        evaluation=evaluation,
        commercial=commercial,
        freshness=fg,
    )

    # Paid research gated
    paid_results = []
    for q in paid_research_questions or []:
        auth = authorize_compliance_research(
            solicitation_id=solicitation_id,
            question=q,
            priority_tier=TIER_1_ACTIVE if pursuit_worthy else TIER_2_QUALIFIED,
        )
        paid_results.append({"question": q, "authorization": auth})

    outreach = {
        "operating_mode": mode,
        "emails_sent": 0,
        "calls_placed": 0,
        "registrations_performed": 0,
        "bids_submitted": 0,
        "financing_applications": 0,
        "agency_contacts": 0,
        "supplier_contacts": 0,
    }

    return {
        "kind": "BidComplianceAnalysis",
        "solicitation_id": solicitation_id,
        "package_map": package_map,
        "requirements": requirements,
        "mandatory_vs_informational": mand_info,
        "compliance_matrix": matrix,
        "product_compliance": prod,
        "brand_or_equal": brand,
        "authorization": authz,
        "origin": origin,
        "registrations": registrations,
        "forms": forms,
        "submission_instructions": submission,
        "evaluation_basis": evaluation,
        "delivery_logistics": delivery,
        "delivery_economics_invalidation": delivery_econ,
        "package_completeness": pkg_complete,
        "bid_readiness": readiness,
        "commercial_verification": commercial,
        "checklist": checklist,
        "freshness": fg,
        "paid_research": paid_results,
        "research_state": research_state or {},
        "outreach": outreach,
        "mode_snapshot": mode_snapshot(),
        "fabricated_requirements": False,
        "analyzed_at": now_utc().isoformat(),
    }


def acknowledge_material_change(
    *,
    analysis: dict[str, Any],
    change_id: str,
    operator_id: str = "operator",
) -> dict[str, Any]:
    out = dict(analysis)
    acks = list(out.get("acknowledged_changes") or [])
    acks.append({"change_id": change_id, "operator_id": operator_id, "at": now_utc().isoformat()})
    out["acknowledged_changes"] = acks
    # Clear unreviewed from readiness path by re-note
    out["operator_ack"] = True
    return out


# Re-export amendment helper for API
process_package_amendment = process_amendment_or_qa
