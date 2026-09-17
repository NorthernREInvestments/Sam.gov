"""Draft bid package assembly + verified-only form population — no signatures/submissions."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from bid_package import FACT_COMPANY_PROPOSED_BID, FACT_GOVERNMENT_VERIFIED, FACT_OPERATOR_SUPPLIED, empty_bid_package
from bid_pricing_constants import (
    COMM_VERIFY,
    DOC_REQUIRED,
    FIELD_UNKNOWN,
    FORM_COMMERCIAL,
    FORM_MISSING,
    FORM_OPERATOR,
    FORM_PARTIAL,
    FORM_READY,
    FORM_SIGNATURE,
    FUTURE_ACTION,
    OP_INPUT,
    REG_REQUIRED,
    SIG_REQUIRED,
)


def populate_field(value: Any, *, verified: bool, field_name: str) -> dict[str, Any]:
    if not verified or value in (None, "", FIELD_UNKNOWN):
        return {
            "field": field_name,
            "value": None,
            "status": OP_INPUT if value in (None, "") else FIELD_UNKNOWN,
            "fabricated": False,
        }
    return {
        "field": field_name,
        "value": value,
        "status": "POPULATED_FROM_VERIFIED_DATA",
        "fabricated": False,
        "verified": True,
    }


def map_required_forms(
    required_forms: list[dict[str, Any]] | None,
    *,
    company_facts: dict[str, Any] | None = None,
    pricing_ready: bool = False,
) -> list[dict[str, Any]]:
    facts = company_facts or {}
    out = []
    for f in required_forms or []:
        key = f.get("form_key") or f.get("name") or "form"
        req = f.get("requirement") or "REQUIRED"
        if req == "REQUIRED" and f.get("form_status") == "FORM_MISSING":
            status = FORM_MISSING
        elif f.get("notary_required"):
            status = FORM_OPERATOR
            sig = True
        elif f.get("signature_required"):
            status = FORM_SIGNATURE
            sig = True
        else:
            sig = False
            # Partial if some company facts present
            fillable = any(facts.get(k) for k in ("legal_name", "uei", "cage", "address"))
            if fillable and pricing_ready:
                status = FORM_PARTIAL
            elif fillable:
                status = FORM_PARTIAL
            else:
                status = FORM_OPERATOR
        out.append(
            {
                "form_key": key,
                "requirement": req,
                "status": status,
                "signature_required": bool(f.get("signature_required") or sig),
                "notary_required": bool(f.get("notary_required")),
                "signature_applied": False,
                "certification_asserted": False,
                "operator_action": FUTURE_ACTION if status != FORM_READY else None,
                "mapped_from": ["company_profile", "solicitation", "bom", "pricing", "delivery", "compliance"],
            }
        )
    if not out:
        # Still account for unknown mandatory forms from compliance
        out.append(
            {
                "form_key": "unknown_mandatory_forms",
                "requirement": "UNKNOWN",
                "status": FORM_OPERATOR,
                "signature_applied": False,
                "certification_asserted": False,
                "operator_action": FUTURE_ACTION,
            }
        )
    return out


def assemble_draft_bid_package(
    *,
    solicitation_id: str,
    solicitation_number: str | None = None,
    company_facts: dict[str, Any] | None = None,
    line_pricing: dict[str, Any] | None = None,
    recommended_bid: dict[str, Any] | None = None,
    submission: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    compliance_matrix: dict[str, Any] | None = None,
    required_forms: list[dict[str, Any]] | None = None,
    product_schedule: list[dict[str, Any]] | None = None,
    commercial_targets: dict[str, Any] | None = None,
    amendments_accounted: bool | None = None,
) -> dict[str, Any]:
    """Assemble draft from verified data only. Never invent/sign/certify/submit."""
    facts = company_facts or {}
    base = empty_bid_package(solicitation_number=solicitation_number or solicitation_id, notice_id=solicitation_id)

    company_section = {
        "legal_name": populate_field(facts.get("legal_name"), verified=bool(facts.get("legal_name_verified")), field_name="legal_name"),
        "uei": populate_field(facts.get("uei"), verified=bool(facts.get("uei")), field_name="uei"),
        "cage": populate_field(facts.get("cage"), verified=bool(facts.get("cage")), field_name="cage"),
        "address": populate_field(facts.get("address"), verified=bool(facts.get("address_verified")), field_name="address"),
        "contact": populate_field(facts.get("contact"), verified=bool(facts.get("contact_verified")), field_name="contact"),
    }

    pricing_lines = (line_pricing or {}).get("lines") or []
    bid_total = (recommended_bid or {}).get("total_bid")
    if bid_total is not None:
        base["operator_proposed_price"] = {
            "value": bid_total,
            "status": "CALCULATED",
            "fact_class": FACT_COMPANY_PROPOSED_BID,
            "notes": "Recommended/scenario bid — not government verified",
        }
    base["pricing_schedule"] = {
        "status": "DRAFT" if pricing_lines else "UNKNOWN",
        "items": pricing_lines,
        "total": bid_total,
        "fact_class": FACT_COMPANY_PROPOSED_BID,
        "reconciliation": (line_pricing or {}).get("reconciliation"),
    }

    forms = map_required_forms(required_forms, company_facts=facts, pricing_ready=bool(pricing_lines))
    base["required_forms"] = {"status": "MAPPED", "items": forms}
    base["signatures"] = {
        "status": SIG_REQUIRED,
        "items": [],
        "signature_applied": False,
        "note": "Never apply digital signature autonomously",
    }
    base["representations_certifications"] = {
        "status": OP_INPUT,
        "items": [],
        "certification_asserted": False,
        "note": "Never attest certifications on operator behalf",
    }

    sub = submission or {}
    base["submission_instructions"] = {
        "status": "KNOWN" if sub.get("submission_method") else FIELD_UNKNOWN,
        "method": sub.get("submission_method") or FIELD_UNKNOWN,
        "deadline": sub.get("deadline") or FIELD_UNKNOWN,
        "timezone": sub.get("timezone"),
        "portal": sub.get("portal"),
        "submitted": False,
    }

    unresolved = []
    for name, field in company_section.items():
        if field["status"] != "POPULATED_FROM_VERIFIED_DATA":
            unresolved.append({"section": "company", "field": name, "status": field["status"]})
    for f in forms:
        if f["status"] not in {FORM_READY}:
            unresolved.append({"section": "forms", "field": f["form_key"], "status": f["status"]})
    unresolved.append({"section": "signatures", "field": "authorized_signature", "status": SIG_REQUIRED})
    if commercial_targets and commercial_targets.get("actions"):
        for a in commercial_targets["actions"]:
            unresolved.append(
                {
                    "section": "commercial_verification",
                    "field": a.get("action"),
                    "status": COMM_VERIFY,
                    "target": a.get("target"),
                }
            )

    matrix_blockers = (compliance_matrix or {}).get("blockers") or []
    for b in matrix_blockers:
        unresolved.append({"section": "compliance", "field": b.get("category") or b.get("requirement"), "status": "BLOCKED"})

    manifest = {
        "kind": "BidPackageManifest",
        "solicitation_id": solicitation_id,
        "sections": [
            "solicitation_identification",
            "bidder_company_information",
            "pricing_schedule",
            "product_schedule",
            "technical_compliance",
            "delivery",
            "certifications_reps",
            "amendment_acknowledgments",
            "required_forms",
            "supporting_documentation",
            "commercial_verification_requirements",
            "unresolved_operator_inputs",
        ],
        "attachments_required": [f["form_key"] for f in forms if f.get("requirement") == "REQUIRED"],
        "amendments_accounted": amendments_accounted,
    }

    draft = {
        "kind": "DraftBidPackage",
        "solicitation_identification": {
            "solicitation_id": solicitation_id,
            "solicitation_number": solicitation_number or solicitation_id,
            "fact_class": FACT_GOVERNMENT_VERIFIED,
        },
        "bidder_company_information": company_section,
        "pricing_schedule": base["pricing_schedule"],
        "product_schedule": product_schedule or pricing_lines,
        "technical_compliance": {"status": FIELD_UNKNOWN, "note": "See compliance matrix"},
        "delivery": delivery or {"status": FIELD_UNKNOWN},
        "certifications_reps": base["representations_certifications"],
        "amendment_acknowledgments": {
            "status": OP_INPUT if amendments_accounted is not True else "ACK_REQUIRED",
            "accounted": amendments_accounted,
            "operator_action": FUTURE_ACTION,
        },
        "required_forms": forms,
        "exceptions_deviations": {"status": "NONE_DECLARED", "items": []},
        "supporting_product_documentation": {"status": DOC_REQUIRED, "items": []},
        "commercial_verification_requirements": commercial_targets or {},
        "unresolved_operator_inputs": unresolved,
        "signatures_applied": False,
        "certifications_asserted": False,
        "bid_submitted": False,
        "portal_submitted": False,
        "fabricated_fields": False,
        "package_manifest": manifest,
        "unresolved_field_manifest": {
            "kind": "UnresolvedFieldManifest",
            "fields": unresolved,
            "operator_must_complete": [u for u in unresolved if u["status"] in {OP_INPUT, SIG_REQUIRED, FORM_SIGNATURE, FORM_OPERATOR}],
        },
        "assembled_at": now_utc().isoformat(),
        "base_bid_package": base,
    }
    return draft


def validate_draft_bid_package(
    draft: dict[str, Any],
    *,
    pricing: dict[str, Any] | None = None,
    freshness_ok: bool = True,
    price_freshness_blocks: bool = False,
    hard_blocker: bool = False,
    deadline_actionable: bool = True,
    product_compliance_ok: bool = True,
    eligibility_ok: bool = True,
    submission_method_known: bool = False,
    pricing_evidence_current: bool = True,
) -> dict[str, Any]:
    blockers = []
    lines = ((draft.get("pricing_schedule") or {}).get("items")) or []
    if not lines:
        blockers.append("pricing_lines_missing")
    recon = (draft.get("pricing_schedule") or {}).get("reconciliation") or (pricing or {}).get("reconciliation") or {}
    if recon and recon.get("totals_reconcile_exactly") is False:
        blockers.append("totals_do_not_reconcile")
    forms = draft.get("required_forms") or []
    if any(f.get("status") == FORM_MISSING for f in forms):
        blockers.append("mandatory_form_missing")
    if draft.get("amendment_acknowledgments", {}).get("accounted") is not True:
        blockers.append("amendments_not_accounted")
    if not freshness_ok:
        blockers.append("package_freshness_failed")
    if price_freshness_blocks or not pricing_evidence_current:
        blockers.append("pricing_evidence_stale_or_expired")
    if hard_blocker:
        blockers.append("unresolved_hard_blocker")
    if not deadline_actionable:
        blockers.append("deadline_not_actionable")
    if not product_compliance_ok:
        blockers.append("product_compliance_unacceptable")
    if not eligibility_ok:
        blockers.append("company_eligibility_unacceptable")
    if not submission_method_known:
        blockers.append("submission_method_unknown")
    if draft.get("signatures_applied"):
        blockers.append("signature_falsely_applied")
    if draft.get("certifications_asserted"):
        blockers.append("certification_falsely_asserted")
    if draft.get("bid_submitted") or draft.get("portal_submitted"):
        blockers.append("submission_not_allowed_in_this_build")

    commercial = draft.get("commercial_verification_requirements") or {}
    if commercial.get("actions"):
        # Not a blocker for draft — identified verification is required
        pass

    return {
        "kind": "DraftBidValidation",
        "passed": len(blockers) == 0,
        "blockers": blockers,
        "signatures_applied": False,
        "certifications_asserted": False,
        "submitted": False,
        "commercial_verification_identified": bool(commercial.get("actions")),
    }
