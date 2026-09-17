"""Solicitation package document vocabulary and manifest records."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

# Retrieval / inventory statuses
KNOWN_RETRIEVED = "KNOWN_RETRIEVED"
KNOWN_NOT_RETRIEVED = "KNOWN_NOT_RETRIEVED"
REFERENCED_NOT_RETRIEVED = "REFERENCED_NOT_RETRIEVED"
POSSIBLE_DOCUMENT = "POSSIBLE_DOCUMENT"
NOT_EXPECTED = "NOT_EXPECTED"
SUPERSEDED = "SUPERSEDED"
RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
UNKNOWN = "UNKNOWN"

RETRIEVAL_STATUSES = frozenset(
    {
        KNOWN_RETRIEVED,
        KNOWN_NOT_RETRIEVED,
        REFERENCED_NOT_RETRIEVED,
        POSSIBLE_DOCUMENT,
        NOT_EXPECTED,
        SUPERSEDED,
        RETRIEVAL_FAILED,
        UNKNOWN,
    }
)

# Canonical document types (uppercase API; map to existing lowercase where needed)
DOC_TYPE_NOTICE = "NOTICE"
DOC_TYPE_BASE_SOLICITATION = "BASE_SOLICITATION"
DOC_TYPE_RFQ = "RFQ"
DOC_TYPE_RFP = "RFP"
DOC_TYPE_IFB = "IFB"
DOC_TYPE_ATTACHMENT = "ATTACHMENT"
DOC_TYPE_SPECIFICATION = "SPECIFICATION"
DOC_TYPE_SOW = "STATEMENT_OF_WORK"
DOC_TYPE_PWS = "PERFORMANCE_WORK_STATEMENT"
DOC_TYPE_BOM = "BOM"
DOC_TYPE_CLIN = "CLIN_SCHEDULE"
DOC_TYPE_PRICING = "PRICING_SHEET"
DOC_TYPE_BRAND_JUST = "BRAND_NAME_JUSTIFICATION"
DOC_TYPE_AMENDMENT = "AMENDMENT"
DOC_TYPE_QA = "Q_AND_A"
DOC_TYPE_REPS = "REPRESENTATIONS_CERTIFICATIONS"
DOC_TYPE_CLAUSE = "CLAUSE_DOCUMENT"
DOC_TYPE_DRAWING = "DRAWING"
DOC_TYPE_TECH = "TECHNICAL_ATTACHMENT"
DOC_TYPE_SUBMISSION = "SUBMISSION_FORM"
DOC_TYPE_PP = "PAST_PERFORMANCE_FORM"
DOC_TYPE_OEM = "OEM_REQUIREMENT"
DOC_TYPE_DELIVERY = "DELIVERY_SCHEDULE"
DOC_TYPE_OTHER = "OTHER"
DOC_TYPE_APPENDIX = "APPENDIX"
DOC_TYPE_EXHIBIT = "EXHIBIT"

URL_DIRECT_DOCUMENT = "DIRECT_DOCUMENT"
URL_NOTICE_PAGE = "NOTICE_PAGE"
URL_API_ENDPOINT = "API_ENDPOINT"
URL_RESOURCE_PAGE = "RESOURCE_PAGE"
URL_UNKNOWN = "UNKNOWN"

PACKAGE_COMPLETE = "SOLICITATION_PACKAGE_COMPLETE"
PACKAGE_INCOMPLETE = "SOLICITATION_PACKAGE_INCOMPLETE"
PACKAGE_UNRESOLVED = "SOLICITATION_PACKAGE_UNRESOLVED"

FINAL_CLARIFICATION_REVIEW_REQUIRED = "FINAL_CLARIFICATION_REVIEW_REQUIRED"


def utc_now_iso() -> str:
    return now_utc().isoformat()


def manifest_entry(
    *,
    document_type: str,
    title: str | None = None,
    filename: str | None = None,
    source_url: str | None = None,
    source_system: str | None = None,
    document_identifier: str | None = None,
    version: str | None = None,
    amendment_number: str | None = None,
    publication_date: str | None = None,
    retrieved_at: str | None = None,
    content_hash: str | None = None,
    parent_document: str | None = None,
    referenced_by: str | None = None,
    required_for_package_completeness: bool = False,
    retrieval_status: str = UNKNOWN,
    text_extraction_status: str = UNKNOWN,
    review_status: str = UNKNOWN,
    superseded_by: str | None = None,
    current_version: bool = True,
    evidence: str | None = None,
    provenance: str | None = None,
    local_attachment_id: int | None = None,
) -> dict[str, Any]:
    st = retrieval_status if retrieval_status in RETRIEVAL_STATUSES else UNKNOWN
    return {
        "document_type": str(document_type or DOC_TYPE_OTHER).upper(),
        "title": title,
        "filename": filename,
        "source_url": source_url,
        "source_system": source_system,
        "document_identifier": document_identifier,
        "version": version,
        "amendment_number": amendment_number,
        "publication_date": publication_date,
        "retrieved_at": retrieved_at,
        "hash": content_hash,
        "parent_document": parent_document,
        "referenced_by": referenced_by,
        "required_for_package_completeness": bool(required_for_package_completeness),
        "retrieval_status": st,
        "text_extraction_status": text_extraction_status,
        "review_status": review_status,
        "superseded_by": superseded_by,
        "current_version": bool(current_version),
        "evidence": evidence,
        "provenance": provenance,
        "local_attachment_id": local_attachment_id,
    }


def classify_url(url: str | None) -> str:
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return URL_UNKNOWN
    if "noticedesc" in u or "/opportunities/v1/" in u:
        return URL_API_ENDPOINT
    if "/resources/files/" in u and "/download" in u:
        return URL_DIRECT_DOCUMENT
    if "/workspace/contract/opp/" in u or "/opp/" in u and "/view" in u:
        return URL_NOTICE_PAGE
    if u.endswith(".pdf") or u.endswith(".docx") or u.endswith(".xlsx"):
        return URL_DIRECT_DOCUMENT
    if "sam.gov" in u:
        return URL_RESOURCE_PAGE
    return URL_UNKNOWN
