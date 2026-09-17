"""Solicitation package completeness — BID_READY never from a single PDF alone."""

from __future__ import annotations

from typing import Any

PACKAGE_COMPLETE = "SOLICITATION_PACKAGE_COMPLETE"
PACKAGE_INCOMPLETE = "SOLICITATION_PACKAGE_INCOMPLETE"
PACKAGE_UNRESOLVED = "SOLICITATION_PACKAGE_UNRESOLVED"

DOC_TYPES = (
    "notice",
    "base_solicitation",
    "rfq_rfp_ifb",
    "attachment",
    "specification",
    "bom_clin_schedule",
    "brand_name_justification",
    "terms_conditions",
    "reps_certs",
    "amendment",
    "qa",
    "drawing",
    "pricing_sheet",
    "submission_form",
    "other_incorporated",
)

# Minimum types we expect to identify before claiming COMPLETE for a typical RFQ buy
CORE_IDENTIFICATION_TYPES = frozenset(
    {
        "notice",
        "rfq_rfp_ifb",
    }
)


def document_record(
    *,
    document_type: str,
    source: str | None = None,
    url: str | None = None,
    retrieved_at: str | None = None,
    version_amendment: str | None = None,
    content_hash: str | None = None,
    text_extraction_status: str = "UNKNOWN",
    superseded: bool = False,
    current: bool = True,
    required_for_bid: bool | None = None,
    review_status: str = "UNKNOWN",
    filename: str | None = None,
    local_attachment_id: int | None = None,
) -> dict[str, Any]:
    return {
        "document_type": str(document_type or "other_incorporated"),
        "source": source,
        "url": url,
        "retrieved_at": retrieved_at,
        "version_amendment": version_amendment,
        "hash": content_hash,
        "text_extraction_status": text_extraction_status,
        "superseded": bool(superseded),
        "current": bool(current),
        "required_for_bid": required_for_bid,
        "review_status": review_status,
        "filename": filename,
        "local_attachment_id": local_attachment_id,
    }


def evaluate_solicitation_package(
    documents: list[dict[str, Any]] | None,
    *,
    amendments_expected: bool | None = None,
    amendments_accounted: bool | None = None,
    required_types_missing: list[str] | None = None,
) -> dict[str, Any]:
    """
    COMPLETE only when package inventory is resolved AND required docs present.
    One arbitrary PDF alone is never COMPLETE.
    Unresolved amendments block COMPLETE and feed BID_READY blockers.
    """
    docs = [d for d in (documents or []) if isinstance(d, dict)]
    current_docs = [d for d in docs if d.get("current") and not d.get("superseded")]
    types_present = {str(d.get("document_type") or "") for d in current_docs}
    blockers: list[str] = []

    if not current_docs:
        return {
            "status": PACKAGE_UNRESOLVED,
            "blockers": ["no_current_documents"],
            "document_count": 0,
            "types_present": [],
            "amendments_accounted": amendments_accounted,
        }

    # Single PDF without typed inventory => UNRESOLVED (not COMPLETE)
    if len(current_docs) == 1 and "rfq_rfp_ifb" in types_present and not (
        "notice" in types_present or amendments_accounted is True
    ):
        blockers.append("single_document_insufficient_for_complete")

    if amendments_expected is True and amendments_accounted is not True:
        blockers.append("amendments_unresolved")

    if amendments_accounted is None and any(
        str(d.get("document_type")) == "amendment" for d in docs
    ):
        # Have amendments but accounting unknown
        blockers.append("amendment_accounting_unknown")

    missing = list(required_types_missing or [])
    for t in CORE_IDENTIFICATION_TYPES:
        # notice may be SAM metadata without a file — do not invent; mark unresolved if neither present
        pass

    # Without explicit required-type checklist resolution, stay UNRESOLVED
    checklist_resolved = required_types_missing is not None and amendments_accounted is not None
    if not checklist_resolved:
        blockers.append("package_checklist_unresolved")

    if missing:
        blockers.append("required_document_types_missing:" + ",".join(missing))

    if blockers:
        # Prefer INCOMPLETE when we know docs exist but gaps; UNRESOLVED when checklist unknown
        status = (
            PACKAGE_INCOMPLETE
            if missing or amendments_expected is True
            else PACKAGE_UNRESOLVED
        )
        if "amendments_unresolved" in blockers:
            status = PACKAGE_INCOMPLETE
        return {
            "status": status,
            "blockers": blockers,
            "document_count": len(current_docs),
            "types_present": sorted(types_present),
            "amendments_accounted": amendments_accounted,
        }

    if not CORE_IDENTIFICATION_TYPES.issubset(types_present) and "rfq_rfp_ifb" not in types_present:
        return {
            "status": PACKAGE_INCOMPLETE,
            "blockers": ["core_solicitation_document_missing"],
            "document_count": len(current_docs),
            "types_present": sorted(types_present),
            "amendments_accounted": amendments_accounted,
        }

    return {
        "status": PACKAGE_COMPLETE,
        "blockers": [],
        "document_count": len(current_docs),
        "types_present": sorted(types_present),
        "amendments_accounted": amendments_accounted,
    }
