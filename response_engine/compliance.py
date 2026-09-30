"""Compliance matrix + hard-block logic — fail closed on material UNKNOWN/FAIL."""

from __future__ import annotations

from typing import Any

from response_engine.constants import (
    AT_SUBMISSION,
    BLOCKED,
    CLARIFICATION_REQUIRED,
    CLARIFICATION_REQUIRED_STATUS,
    COMPLIANCE_REVIEW,
    FAIL,
    MATERIAL,
    NOT_APPLICABLE,
    PACKAGE_STALE_DUE_TO_AMENDMENT,
    PASS,
    POTENTIALLY_MATERIAL,
    PRE_AWARD,
    PRE_AWARD_RESOLUTION_ALLOWED,
    READY_FOR_RESPONSE_BUILD,
    REVIEW_REQUIRED,
    UNKNOWN,
)
from response_engine.models import new_hard_block


def set_requirement_answer(
    req: dict[str, Any],
    *,
    answer_value: Any,
    answer_status: str,
    compliance_status: str,
    evidence_ids: list[str] | None = None,
    namespace: str | None = None,
    na_rationale: str | None = None,
) -> dict[str, Any]:
    if answer_status == NOT_APPLICABLE and not na_rationale:
        raise ValueError("NOT_APPLICABLE requires rationale/evidence")
    # Never allow INTERNAL namespace answers to become PASS for submission claims
    if namespace == "INTERNAL_EVIDENCE" and compliance_status == PASS:
        compliance_status = REVIEW_REQUIRED
        req["notes"] = (req.get("notes") or "") + " Internal evidence cannot alone create PASS."
    req["answer_value"] = answer_value
    req["answer_status"] = answer_status
    req["compliance_status"] = compliance_status
    req["answer_evidence_ids"] = evidence_ids or []
    req["answer_namespace"] = namespace
    if na_rationale:
        req["na_rationale"] = na_rationale
    return req


def evaluate_exact_brand_mismatch(
    req: dict[str, Any],
    *,
    required_model: str | None,
    offered_model: str | None,
) -> dict[str, Any]:
    """Exact brand/model only — mismatch is FAIL, not review."""
    if req.get("requirement_category") != "EXACT_BRAND":
        return req
    if not required_model or not offered_model:
        return set_requirement_answer(req, answer_value=offered_model, answer_status="UNKNOWN", compliance_status=UNKNOWN)
    if required_model.strip().lower() != offered_model.strip().lower():
        return set_requirement_answer(
            req,
            answer_value=offered_model,
            answer_status="ANSWERED_VERIFIED",
            compliance_status=FAIL,
            namespace="VERIFIED_RESPONSE_FACTS",
        )
    return set_requirement_answer(
        req,
        answer_value=offered_model,
        answer_status="ANSWERED_VERIFIED",
        compliance_status=PASS,
        namespace="VERIFIED_RESPONSE_FACTS",
    )


def evaluate_salient_generic_equal_claim(req: dict[str, Any], claim: str | None) -> dict[str, Any]:
    """Generic 'our product is equal' must NOT create PASS."""
    if req.get("requirement_category") != "SALIENT_CHARACTERISTIC":
        return req
    c = (claim or "").lower()
    if "equal" in c and not any(k in c for k in ("capacity", "size", "voltage", "warranty", "spec", "measured", "tested")):
        return set_requirement_answer(
            req,
            answer_value=claim,
            answer_status="ANSWERED_UNVERIFIED",
            compliance_status=REVIEW_REQUIRED,
            namespace="INTERNAL_EVIDENCE",
        )
    return req


def build_compliance_matrix(project: dict[str, Any]) -> dict[str, Any]:
    rows = []
    summary = {
        "total_requirements": 0,
        "mandatory_requirements": 0,
        "passed": 0,
        "failed": 0,
        "unknown": 0,
        "review_required": 0,
        "clarification_required": 0,
        "material_unresolved": 0,
        "nonmaterial_unresolved": 0,
        "superseded": 0,
    }
    for req in project.get("requirements") or []:
        if req.get("superseded") or req.get("answer_status") == "SUPERSEDED":
            summary["superseded"] += 1
            continue
        summary["total_requirements"] += 1
        if req.get("mandatory"):
            summary["mandatory_requirements"] += 1
        status = req.get("compliance_status") or UNKNOWN
        row = {
            "requirement_id": req["requirement_id"],
            "requirement": req.get("normalized_requirement") or req.get("requirement_text"),
            "category": req.get("requirement_category"),
            "source": _source_label(req),
            "mandatory": bool(req.get("mandatory")),
            "materiality": req.get("materiality"),
            "timing": req.get("timing"),
            "curability": req.get("curability"),
            "our_answer": req.get("answer_value"),
            "answer_status": req.get("answer_status"),
            "evidence": req.get("answer_evidence_ids") or [],
            "status": status,
            "confidence": req.get("confidence"),
        }
        rows.append(row)
        if status == PASS:
            summary["passed"] += 1
        elif status == FAIL:
            summary["failed"] += 1
        elif status == REVIEW_REQUIRED:
            summary["review_required"] += 1
        elif status == CLARIFICATION_REQUIRED_STATUS:
            summary["clarification_required"] += 1
        else:
            summary["unknown"] += 1

        unresolved = status in {UNKNOWN, REVIEW_REQUIRED, CLARIFICATION_REQUIRED_STATUS, FAIL}
        material = req.get("materiality") in {MATERIAL, POTENTIALLY_MATERIAL, UNKNOWN}
        # Pre-award allowed requirements are not AT_SUBMISSION hard failures
        pre_award_ok = (
            req.get("timing") == PRE_AWARD
            or req.get("curability") == PRE_AWARD_RESOLUTION_ALLOWED
        )
        if unresolved and req.get("mandatory") and material and not (pre_award_ok and status != FAIL):
            if pre_award_ok and status in {UNKNOWN, REVIEW_REQUIRED}:
                # Track but do not count as submission hard-block material unresolved
                summary["nonmaterial_unresolved"] += 1  # bucket as non-submission-blocking for summary
            else:
                summary["material_unresolved"] += 1
        elif unresolved and not material:
            summary["nonmaterial_unresolved"] += 1

    project["compliance_matrix"] = {"kind": "ComplianceMatrix", "rows": rows, "summary": summary}
    return project["compliance_matrix"]


def compute_hard_blocks(project: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    docs = project.get("documents") or []
    if not docs:
        blocks.append(new_hard_block(
            reason="Controlling documents incomplete — no solicitation documents loaded.",
            source="document_inventory",
            resolution_action="Ingest base solicitation and all amendments/attachments.",
        ))

    if not project.get("submission_deadline") and not _has_category(project, "SUBMISSION_DEADLINE"):
        # Only block if nowhere captured
        pass
    elif project.get("submission_deadline") and not project.get("submission_timezone"):
        # Check if deadline requirement has timezone in text
        tz_known = False
        for req in project.get("requirements") or []:
            if req.get("requirement_category") == "SUBMISSION_DEADLINE":
                txt = (req.get("requirement_text") or "").upper()
                if any(z in txt for z in ("ET", "EST", "EDT", "CT", "CST", "PT", "UTC", "GMT", "EASTERN", "CENTRAL", "PACIFIC")):
                    tz_known = True
                    project["submission_timezone"] = project.get("submission_timezone") or "INFERRED_FROM_TEXT"
        if not tz_known and not project.get("submission_timezone"):
            blocks.append(new_hard_block(
                reason="Submission deadline timezone unresolved.",
                source="SUBMISSION_DEADLINE",
                resolution_action="Confirm timezone from solicitation or owner review.",
            ))

    if not project.get("submission_system") or project.get("submission_system") == "UNKNOWN":
        if not _has_answered_pass(project, "SUBMISSION_METHOD"):
            blocks.append(new_hard_block(
                reason="Submission method unresolved.",
                source="submission_system",
                resolution_action="Identify portal/email/physical submission path from controlling documents.",
            ))

    for amd in project.get("amendments") or []:
        if amd.get("acknowledgment_required") and amd.get("acknowledgment_status") != "ACKNOWLEDGED":
            blocks.append(new_hard_block(
                reason=f"Amendment {amd.get('number')} acknowledgment unresolved.",
                source=amd.get("amendment_id"),
                resolution_action="Owner must acknowledge amendment before submission (external ack later).",
            ))

    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        status = req.get("compliance_status") or UNKNOWN
        material = req.get("materiality") in {MATERIAL, POTENTIALLY_MATERIAL}
        mandatory = req.get("mandatory")
        pre_award_ok = req.get("timing") == PRE_AWARD or req.get("curability") == PRE_AWARD_RESOLUTION_ALLOWED
        if not mandatory or not material:
            continue
        if pre_award_ok and status in {UNKNOWN, REVIEW_REQUIRED}:
            continue  # not an AT_SUBMISSION hard failure
        if status == FAIL:
            blocks.append(new_hard_block(
                reason=f"Mandatory material requirement FAIL: {req.get('requirement_category')}",
                source=req.get("requirement_id"),
                resolution_action="Correct offered product/answer or withdraw.",
                requirement_id=req["requirement_id"],
            ))
        elif status == UNKNOWN and req.get("timing") in {AT_SUBMISSION, None}:
            blocks.append(new_hard_block(
                reason=f"Mandatory material requirement UNKNOWN: {req.get('requirement_category')}",
                source=req.get("requirement_id"),
                resolution_action="Obtain verified evidence or owner confirmation before any response claim.",
                requirement_id=req["requirement_id"],
            ))
        elif status == FAIL and req.get("requirement_category") == "SIGNATURE":
            blocks.append(new_hard_block(
                reason="Required signature missing.",
                source=req.get("requirement_id"),
                resolution_action="Obtain authorized signature on required form/certification.",
                requirement_id=req["requirement_id"],
            ))

    # Signature specifically unanswered
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        if req.get("requirement_category") == "SIGNATURE" and req.get("mandatory"):
            if (req.get("compliance_status") or UNKNOWN) in {UNKNOWN, FAIL} and req.get("answer_status") in {
                "UNANSWERED", "UNKNOWN", None,
            }:
                if not any(b.get("requirement_id") == req["requirement_id"] for b in blocks):
                    blocks.append(new_hard_block(
                        reason="Required signed certification/form missing.",
                        source=req.get("requirement_id"),
                        resolution_action="Collect signed form before submission packaging.",
                        requirement_id=req["requirement_id"],
                    ))

    if project.get("clarifications"):
        open_c = [c for c in project["clarifications"] if c.get("resolution_status") == "OPEN" and c.get("materiality") == MATERIAL]
        for c in open_c:
            blocks.append(new_hard_block(
                reason=f"Open material clarification: {c.get('issue')}",
                source=c.get("clarification_id"),
                resolution_action="Resolve via owner-authorized buyer question (do not auto-contact).",
            ))

    # Dedupe by reason+source
    seen = set()
    uniq = []
    for b in blocks:
        key = (b.get("reason"), b.get("source"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)

    project["hard_blocks"] = uniq
    project["hard_block_count"] = len(uniq)
    matrix = project.get("compliance_matrix") or {}
    summary = matrix.get("summary") or {}
    project["unresolved_material_requirement_count"] = int(summary.get("material_unresolved") or 0)

    # Status derivation — never READY if material unresolved
    if project.get("package_status") == PACKAGE_STALE_DUE_TO_AMENDMENT and uniq:
        project["response_status"] = BLOCKED
    elif uniq:
        if any("clarification" in (b.get("reason") or "").lower() for b in uniq):
            project["response_status"] = CLARIFICATION_REQUIRED
        else:
            project["response_status"] = BLOCKED
    elif int(summary.get("material_unresolved") or 0) > 0:
        project["response_status"] = COMPLIANCE_REVIEW
        project["owner_review_required"] = True
    elif int(summary.get("review_required") or 0) > 0:
        project["response_status"] = COMPLIANCE_REVIEW
        project["owner_review_required"] = True
    elif summary.get("total_requirements", 0) > 0 and summary.get("unknown", 0) == 0 and summary.get("failed", 0) == 0:
        project["response_status"] = READY_FOR_RESPONSE_BUILD
    else:
        project["response_status"] = COMPLIANCE_REVIEW

    return uniq


def operator_compliance_summary(project: dict[str, Any]) -> dict[str, Any]:
    """Plain-language Bid Prep summary — no technical jargon."""
    s = (project.get("compliance_matrix") or {}).get("summary") or {}
    blocks = project.get("hard_blocks") or []
    clar = [c for c in project.get("clarifications") or [] if c.get("resolution_status") == "OPEN"]
    docs = project.get("documents") or []
    controlling = [d for d in docs if d.get("controlling_status") == "CONTROLLING"]
    next_action = "Continue document intake"
    if blocks:
        next_action = blocks[0].get("resolution_action") or "Resolve hard blockers"
    elif clar:
        next_action = "Resolve clarification before question deadline"
    elif int(s.get("material_unresolved") or 0) > 0:
        next_action = "Resolve missing verified evidence for material requirements"
    elif project.get("response_status") == READY_FOR_RESPONSE_BUILD:
        next_action = "Ready for response build (R2+)"

    return {
        "response_type": project.get("response_type"),
        "evaluation_method": project.get("evaluation_method"),
        "documents_loaded": len(docs),
        "documents_controlling": len(controlling),
        "amendments": len(project.get("amendments") or []),
        "requirements_found": s.get("total_requirements", 0),
        "satisfied": s.get("passed", 0),
        "need_data": s.get("unknown", 0),
        "need_owner_review": s.get("review_required", 0),
        "hard_blockers": len(blocks),
        "clarifications_open": len(clar),
        "material_unresolved": s.get("material_unresolved", 0),
        "status": project.get("response_status"),
        "next_action": next_action,
        "ready_claim_forbidden": True,  # never show 100% compliant in R1
        "plain": {
            "documents": f"{len(controlling)}/{len(docs)} controlling loaded" if docs else "No documents loaded",
            "requirements": f"{s.get('total_requirements', 0)} found",
            "compliance": (
                f"{s.get('passed', 0)} satisfied · "
                f"{s.get('unknown', 0)} need data · "
                f"{s.get('review_required', 0)} need owner review · "
                f"{len(blocks)} hard blockers"
            ),
            "questions": (
                f"{len(clar)} ambiguity must be resolved"
                + (f" before {clar[0].get('question_deadline')}" if clar and clar[0].get("question_deadline") else "")
                if clar else "No open clarifications"
            ),
        },
    }


def _source_label(req: dict[str, Any]) -> str:
    parts = []
    if req.get("source_section"):
        parts.append(str(req["source_section"]))
    if req.get("source_page"):
        parts.append(f"p{req['source_page']}")
    if req.get("source_anchor") and req["source_anchor"] not in parts:
        parts.append(str(req["source_anchor"]))
    if not parts and req.get("source_document_id"):
        parts.append(req["source_document_id"][:16])
    return " ".join(parts) or "—"


def _has_category(project: dict[str, Any], category: str) -> bool:
    return any(
        r.get("requirement_category") == category and not r.get("superseded")
        for r in project.get("requirements") or []
    )


def _has_answered_pass(project: dict[str, Any], category: str) -> bool:
    return any(
        r.get("requirement_category") == category and r.get("compliance_status") == PASS and not r.get("superseded")
        for r in project.get("requirements") or []
    )
