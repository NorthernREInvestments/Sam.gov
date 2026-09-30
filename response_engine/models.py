"""R1 canonical data models — ResponseProject and related structures."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from application_clock import now_utc

from response_engine.constants import (
    ANSWER_UNKNOWN,
    AT_SUBMISSION,
    BUILD,
    MATERIAL,
    NOT_SAFE_TO_ASSUME_CURABLE,
    NOT_STARTED,
    NS_INTERNAL_EVIDENCE,
    NS_VERIFIED_RESPONSE_FACTS,
    NS_GOVERNMENT_SUBMISSION,
    UNKNOWN,
    UNKNOWN_CURABILITY,
    UNKNOWN_EVALUATION,
    UNKNOWN_MATERIALITY,
    UNKNOWN_RESPONSE_TYPE,
    UNKNOWN_TIMING,
    UNANSWERED,
)


def _utc() -> str:
    return now_utc().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def content_hash(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def empty_provenance(
    *,
    document_id: str | None = None,
    source_page: str | int | None = None,
    source_section: str | None = None,
    source_anchor: str | None = None,
    excerpt: str | None = None,
    extractor: str = "r1_deterministic",
    confidence: str = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "source_page": source_page,
        "source_section": source_section,
        "source_anchor": source_anchor,
        "excerpt": (excerpt or "")[:500] or None,
        "extractor": extractor,
        "confidence": confidence,
        "extracted_at": _utc(),
    }


def new_response_project(
    *,
    canonical_opportunity_id: str | None = None,
    buyer: str | None = None,
    solicitation_number: str | None = None,
    title: str | None = None,
    jurisdiction: str | None = None,
    discovery_source: str | None = None,
    authoritative_source: str | None = None,
    submission_system: str | None = None,
) -> dict[str, Any]:
    rid = new_id("RP")
    now = _utc()
    return {
        "kind": "ResponseProject",
        "build": BUILD,
        "response_project_id": rid,
        "canonical_opportunity_id": canonical_opportunity_id,
        "buyer": buyer,
        "solicitation_number": solicitation_number,
        "title": title,
        "jurisdiction": jurisdiction,
        "acquisition_type": None,
        "response_type": UNKNOWN_RESPONSE_TYPE,
        "response_type_evidence": [],
        "evaluation_method": [UNKNOWN_EVALUATION],
        "evaluation_evidence": [],
        "discovery_source": discovery_source,
        "authoritative_source": authoritative_source,
        "submission_system": submission_system,
        "response_status": NOT_STARTED,
        "created_at": now,
        "updated_at": now,
        "current_controlling_version": None,
        "question_deadline": None,
        "submission_deadline": None,
        "submission_timezone": None,
        "internal_submission_target": None,
        "owner_review_required": False,
        "hard_block_count": 0,
        "unresolved_material_requirement_count": 0,
        "product_mode": None,
        "product_mode_evidence": None,
        "federal_sections_present": [],
        "documents": [],
        "amendments": [],
        "document_graph": {"edges": [], "conflicts": []},
        "requirements": [],
        "compliance_matrix": {"rows": [], "summary": {}},
        "clarifications": [],
        "hard_blocks": [],
        "deliverables": [],
        "evidence_namespaces": {
            NS_INTERNAL_EVIDENCE: [],
            NS_VERIFIED_RESPONSE_FACTS: [],
            NS_GOVERNMENT_SUBMISSION: [],
        },
        "history": [{"at": now, "event": "created"}],
        "package_status": None,
        "company_certification_snapshot_ref": None,
    }


def new_document(
    *,
    response_project_id: str,
    document_type: str,
    title: str | None = None,
    filename: str | None = None,
    source_url: str | None = None,
    authoritative_source: str | None = None,
    source_system: str | None = None,
    text: str | None = None,
    version: str | None = None,
    amendment_number: str | None = None,
    published_at: str | None = None,
    effective_date: str | None = None,
    controlling_status: str = "CONTROLLING",
    page_count: int | None = None,
    is_buyer_template: bool = False,
) -> dict[str, Any]:
    raw = text or ""
    ch = content_hash(raw) if raw else content_hash(f"{filename}|{source_url}|{title}")
    return {
        "kind": "SolicitationDocument",
        "document_id": new_id("DOC"),
        "response_project_id": response_project_id,
        "document_type": document_type,
        "title": title or filename or document_type,
        "filename": filename,
        "source_url": source_url,
        "authoritative_source": authoritative_source,
        "source_system": source_system,
        "retrieved_at": _utc(),
        "published_at": published_at,
        "effective_date": effective_date,
        "version": version,
        "amendment_number": amendment_number,
        "content_hash": ch,
        "file_hash": ch,
        "supersedes_document_id": None,
        "superseded_by_document_id": None,
        "controlling_status": controlling_status,
        "parse_status": "TEXT_AVAILABLE" if raw.strip() else "METADATA_ONLY",
        "extraction_confidence": "HIGH" if raw.strip() else "LOW",
        "page_count": page_count,
        "text": raw,
        "is_buyer_template": is_buyer_template,
        "provenance_refs": [],
    }


def new_amendment(
    *,
    number: str,
    issued_at: str | None = None,
    acknowledgment_required: bool = True,
    materiality: str = MATERIAL,
    document_id: str | None = None,
    changes: dict[str, bool] | None = None,
) -> dict[str, Any]:
    changes = changes or {}
    return {
        "kind": "AmendmentRecord",
        "amendment_id": new_id("AMD"),
        "number": str(number),
        "issued_at": issued_at,
        "document_id": document_id,
        "acknowledgment_required": acknowledgment_required,
        "materiality": materiality,
        "changes_deadline": bool(changes.get("deadline")),
        "changes_quantity": bool(changes.get("quantity")),
        "changes_price_structure": bool(changes.get("price")),
        "changes_specification": bool(changes.get("specification")),
        "changes_delivery": bool(changes.get("delivery")),
        "changes_evaluation": bool(changes.get("evaluation")),
        "changes_submission_method": bool(changes.get("submission")),
        "changes_required_documents": bool(changes.get("documents")),
        "changes_forms": bool(changes.get("forms")),
        "changes_other": bool(changes.get("other")),
        "owner_acknowledged": False,
        "acknowledgment_method": None,
        "acknowledgment_status": "UNACKNOWLEDGED",
    }


def new_requirement(
    *,
    response_project_id: str,
    requirement_text: str,
    requirement_category: str,
    source_document_id: str | None,
    mandatory: bool = True,
    materiality: str = MATERIAL,
    timing: str = AT_SUBMISSION,
    curability: str = NOT_SAFE_TO_ASSUME_CURABLE,
    provenance: dict[str, Any] | None = None,
    expected_response_type: str | None = None,
    applies_to_clin: str | None = None,
    applies_to_product: bool = False,
    applies_to_company: bool = False,
    evidence_required: bool = True,
    owner_confirmation_required: bool = False,
    amendment_version: str | None = None,
    confidence: str = "MEDIUM",
    normalized_requirement: str | None = None,
) -> dict[str, Any]:
    prov = provenance or empty_provenance(document_id=source_document_id, excerpt=requirement_text)
    text = requirement_text.strip()
    return {
        "kind": "SolicitationRequirement",
        "requirement_id": new_id("REQ"),
        "response_project_id": response_project_id,
        "requirement_text": text,
        "normalized_requirement": normalized_requirement or _normalize(text),
        "requirement_category": requirement_category,
        "mandatory": mandatory,
        "materiality": materiality if materiality != UNKNOWN_MATERIALITY else (
            MATERIAL if mandatory else UNKNOWN_MATERIALITY
        ),
        "timing": timing or UNKNOWN_TIMING,
        "curability": curability or UNKNOWN_CURABILITY,
        "source_document_id": source_document_id,
        "source_page": prov.get("source_page"),
        "source_section": prov.get("source_section"),
        "source_anchor": prov.get("source_anchor"),
        "provenance": prov,
        "applies_to_clin": applies_to_clin,
        "applies_to_product": applies_to_product,
        "applies_to_company": applies_to_company,
        "expected_response_type": expected_response_type,
        "portal_destination": None,
        "form_destination": None,
        "evidence_required": evidence_required,
        "owner_confirmation_required": owner_confirmation_required,
        "answer_status": UNANSWERED,
        "answer_value": None,
        "answer_evidence_ids": [],
        "answer_namespace": None,  # must be VERIFIED or SUBMISSION — never INTERNAL for submission
        "compliance_status": UNKNOWN,
        "confidence": confidence,
        "amendment_version": amendment_version,
        "superseded": False,
        "superseded_by_requirement_id": None,
        "notes": None,
        "na_rationale": None,
    }


def new_hard_block(
    *,
    reason: str,
    source: str | None = None,
    resolution_action: str,
    requirement_id: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "ResponseHardBlock",
        "block_id": new_id("BLK"),
        "reason": reason,
        "source": source,
        "resolution_action": resolution_action,
        "requirement_id": requirement_id,
        "created_at": _utc(),
    }


def new_clarification(
    *,
    issue: str,
    sources: list[str] | None = None,
    materiality: str = MATERIAL,
    question_deadline: str | None = None,
    recommended_question: str | None = None,
    buyer_contact: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "ClarificationIssue",
        "clarification_id": new_id("CLR"),
        "issue": issue,
        "sources": sources or [],
        "materiality": materiality,
        "question_deadline": question_deadline,
        "recommended_question": recommended_question or f"Please clarify: {issue}",
        "buyer_contact": buyer_contact,
        "resolution_status": "OPEN",
        "auto_contact_forbidden": True,
        "created_at": _utc(),
    }


def new_deliverable(
    *,
    dtype: str,
    required: bool = True,
    source_requirement_id: str | None = None,
    due_timing: str = AT_SUBMISSION,
    format_hint: str | None = None,
    filename_requirement: str | None = None,
    signature_requirement: bool = False,
    upload_destination: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "SubmissionDeliverable",
        "deliverable_id": new_id("DLV"),
        "type": dtype,
        "label": label or dtype,
        "required": required,
        "source_requirement_id": source_requirement_id,
        "due_timing": due_timing,
        "format": format_hint,
        "filename_requirement": filename_requirement,
        "signature_requirement": signature_requirement,
        "upload_destination": upload_destination,
        "status": "NOT_STARTED",
    }


def store_internal_evidence(project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Place facts in INTERNAL namespace — never auto-copy to submission."""
    row = {
        "evidence_id": new_id("IEV"),
        "namespace": NS_INTERNAL_EVIDENCE,
        "payload": deepcopy(payload),
        "created_at": _utc(),
        "may_enter_submission": False,
    }
    project.setdefault("evidence_namespaces", {}).setdefault(NS_INTERNAL_EVIDENCE, []).append(row)
    return row


def promote_to_verified_fact(
    project: dict[str, Any],
    *,
    fact: dict[str, Any],
    owner_confirmed: bool = False,
) -> dict[str, Any]:
    row = {
        "evidence_id": new_id("VRF"),
        "namespace": NS_VERIFIED_RESPONSE_FACTS,
        "payload": deepcopy(fact),
        "owner_confirmed": owner_confirmed,
        "created_at": _utc(),
    }
    project.setdefault("evidence_namespaces", {}).setdefault(NS_VERIFIED_RESPONSE_FACTS, []).append(row)
    return row


def assert_not_internal_in_submission(project: dict[str, Any]) -> list[str]:
    """Detect accidental leakage of internal fields into submission namespace."""
    leaks: list[str] = []
    forbidden = {
        "max_buy", "margin", "target_profit", "expected_profit", "financing",
        "subject_to_change", "internal_notes", "supplier_boilerplate",
    }
    for row in (project.get("evidence_namespaces") or {}).get(NS_GOVERNMENT_SUBMISSION) or []:
        blob = str(row.get("payload") or {}).lower()
        for f in forbidden:
            if f.replace("_", " ") in blob or f in blob:
                leaks.append(f"INTERNAL field '{f}' found in GOVERNMENT_SUBMISSION_CONTENT")
    return leaks


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
