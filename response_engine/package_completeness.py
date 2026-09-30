"""Document package completeness + referenced attachment discovery."""

from __future__ import annotations

import re
from typing import Any

from response_engine.constants import MATERIAL
from response_engine.models import new_clarification, new_hard_block

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
MISSING_REFERENCED_DOCUMENT = "MISSING_REFERENCED_DOCUMENT"
AUTH_REQUIRED = "AUTH_REQUIRED"
FETCH_BLOCKED = "FETCH_BLOCKED"
PARSE_REVIEW_REQUIRED = "PARSE_REVIEW_REQUIRED"
UNKNOWN = "UNKNOWN"
OCR_REQUIRED = "OCR_REQUIRED"

_REF_PATTERNS = [
    r"\bAttachment\s+([A-Z0-9]+)\b",
    r"\bExhibit\s+([A-Z0-9]+)\b",
    r"\bAppendix\s+([A-Z0-9]+)\b",
    r"\bSchedule\s+([A-Z0-9]+)\b",
    r"\bAddendum\s+(\d+)\b",
    r"\bAmendment\s+(?:No\.?\s*)?(\d+)\b",
    r"\b(?:see|refer(?:ence)?\s+to)\s+(Attachment|Exhibit|Appendix|Schedule)\s+([A-Z0-9]+)\b",
    r"\bPricing\s+(?:Sheet|Workbook|Schedule)\b",
    r"\bCost\s+Sheet\b",
    r"\bBid\s+Form\b",
    r"\bOffer\s+Form\b",
    r"\bTechnical\s+Specifications?\b",
    r"\bTerms\s+and\s+Conditions\b",
    r"\bStandard\s+Terms\b",
    r"\bQuestions?\s*(?:&|and)\s*Answers?\b",
    r"\bBidder\s+Response\s+Form\b",
    r"\bMaster\s+Solicitation\b",
    r"\bDrawing\s+[A-Z0-9\-]+\b",
    r"\bCertification\s+Form\b",
]


def classify_reference_role(label: str) -> str:
    t = (label or "").lower()
    if any(x in t for x in ("pricing", "cost sheet", "bid form", "offer form", "certif", "sf 14", "sf1449")):
        return "REQUIRED_FOR_RESPONSE"
    if any(x in t for x in ("specification", "drawing", "master solicitation", "terms and conditions")):
        return "REQUIRED_FOR_COMPLIANCE"
    if any(x in t for x in ("question", "q&a", "informational")):
        return "INFORMATIONAL"
    if "attachment" in t or "exhibit" in t or "appendix" in t:
        return "UNKNOWN"
    return "REFERENCE_ONLY"


def discover_referenced_attachments(text: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pat in _REF_PATTERNS:
        for m in re.finditer(pat, text or "", re.I):
            label = m.group(0).strip()
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "label": label,
                    "excerpt": (text[max(0, m.start() - 30) : m.end() + 60]).replace("\n", " ")[:160],
                    "source_span": [m.start(), m.end()],
                    "role": classify_reference_role(label),
                }
            )
    return refs


def referenced_docs_accounted_for(
    references: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (found, missing) by fuzzy filename/title match."""
    titles = " ".join(
        f"{d.get('title') or ''} {d.get('filename') or ''} {d.get('document_type') or ''}".lower()
        for d in documents
    )
    found, missing = [], []
    for ref in references:
        label = (ref.get("label") or "").lower()
        # Extract token like "attachment c" / "pricing sheet"
        token = re.sub(r"^(see|refer(?:ence)?\s+to)\s+", "", label, flags=re.I).strip()
        # Attachment C → look for "attachment c" or " c " or filename containing C
        hit = False
        if "pricing sheet" in token or "cost sheet" in token:
            hit = any(x in titles for x in ("pricing", "cost sheet", "price schedule", "bid schedule"))
        elif "technical specification" in token:
            hit = "spec" in titles or "specification" in titles
        elif "question" in token and "answer" in token:
            hit = "q_and_a" in titles or "q&a" in titles or "q and a" in titles
        elif "bidder response" in token:
            hit = "bidder" in titles or "response form" in titles
        else:
            m = re.search(r"(attachment|exhibit|appendix|schedule|addendum|amendment)\s+([a-z0-9]+)", token, re.I)
            if m:
                kind, num = m.group(1).lower(), m.group(2).lower()
                hit = (f"{kind} {num}" in titles) or (f"{kind}_{num}" in titles) or (
                    kind == "amendment" and any(
                        str(d.get("amendment_number") or "").lstrip("0") == num.lstrip("0")
                        for d in documents
                    )
                )
            else:
                hit = token[:20] in titles if len(token) > 4 else False
        (found if hit else missing).append(ref)
    return found, missing


def evaluate_package_completeness(project: dict[str, Any]) -> dict[str, Any]:
    docs = project.get("documents") or []
    intake = project.get("intake") or {}
    refs = intake.get("referenced_attachments") or []
    if not refs:
        # recompute from controlling text
        text = "\n".join(d.get("text") or "" for d in docs if d.get("controlling_status") != "SUPERSEDED")
        refs = discover_referenced_attachments(text)
        intake["referenced_attachments"] = refs

    found, missing = referenced_docs_accounted_for(refs, docs)
    fetch_states = intake.get("fetch_results") or []
    auth_required = any(f.get("status") in {AUTH_REQUIRED, "AUTH_REQUIRED", "LOGIN_REQUIRED"} for f in fetch_states)
    fetch_blocked = any(f.get("status") in {"ANTI_BOT_BLOCKED", "FETCH_BLOCKED", FETCH_BLOCKED} for f in fetch_states)
    parse_review = any(
        d.get("extraction_confidence") in {"LOW", "UNUSABLE"} or d.get("parse_status") in {OCR_REQUIRED, "PARSE_REVIEW_REQUIRED"}
        for d in docs
    )
    # OCR flag on document
    parse_review = parse_review or any((d.get("flags") or []) and "OCR_REQUIRED" in (d.get("flags") or []) for d in docs)

    has_base = any(d.get("document_type") == "BASE_SOLICITATION" for d in docs) or bool(docs)
    templates = [d for d in docs if d.get("is_buyer_template")]

    status = COMPLETE
    if auth_required and not docs:
        status = AUTH_REQUIRED
    elif fetch_blocked and not docs:
        status = FETCH_BLOCKED
    elif missing:
        status = MISSING_REFERENCED_DOCUMENT
    elif parse_review:
        status = PARSE_REVIEW_REQUIRED
    elif not has_base:
        status = PARTIAL if docs else UNKNOWN
    elif not docs:
        status = UNKNOWN
    else:
        # amendments accounted? if text references amendments but none loaded
        amd_refs = [r for r in refs if "amendment" in (r.get("label") or "").lower()]
        if amd_refs and not (project.get("amendments") or []):
            status = PARTIAL
        else:
            status = COMPLETE

    # Hard blocks for missing material references
    blocks_added = []
    if missing and status == MISSING_REFERENCED_DOCUMENT:
        for mref in missing:
            role = mref.get("role") or "UNKNOWN"
            # Informational/reference-only missing refs → review, not hard block
            if role in {"INFORMATIONAL", "REFERENCE_ONLY"}:
                project.setdefault("clarifications", []).append(
                    new_clarification(
                        issue=f"Referenced document not found ({role}): {mref.get('label')}",
                        sources=[mref.get("excerpt") or ""],
                        materiality="POTENTIALLY_MATERIAL",
                        recommended_question=f"Confirm whether '{mref.get('label')}' is required.",
                    )
                )
                continue
            blk = new_hard_block(
                reason=f"REFERENCED_DOCUMENT_MISSING: {mref.get('label')}",
                source=mref.get("excerpt"),
                resolution_action=f"Locate or upload '{mref.get('label')}' from authoritative source.",
            )
            blocks_added.append(blk)
        # Only treat as incomplete COMPLETE-blocker when material/required missing
        material_missing = [m for m in missing if (m.get("role") or "UNKNOWN") not in {"INFORMATIONAL", "REFERENCE_ONLY"}]
        project.setdefault("intake_missing_refs", material_missing or missing)
        if not material_missing and status == MISSING_REFERENCED_DOCUMENT:
            status = PARTIAL if docs else UNKNOWN

    complete_flag = (
        status == COMPLETE
        and has_base
        and not any((m.get("role") or "UNKNOWN") not in {"INFORMATIONAL", "REFERENCE_ONLY"} for m in missing)
    )

    if auth_required and not docs:
        project.setdefault("clarifications", []).append(
            new_clarification(
                issue="Authoritative portal requires login — documents not fetched automatically.",
                sources=[project.get("authoritative_source") or ""],
                materiality=MATERIAL,
                recommended_question="Operator must download package from authoritative URL and upload.",
            )
        )

    result = {
        "kind": "DocumentPackageCompleteness",
        "document_package_complete": complete_flag,
        "status": status,
        "documents_loaded": len(docs),
        "references_found": len(refs),
        "references_accounted": len(found),
        "references_missing": missing,
        "buyer_templates": len(templates),
        "auth_required": auth_required,
        "fetch_blocked": fetch_blocked,
        "parse_review_required": parse_review,
        "has_base_solicitation": has_base,
        "blocks_preview": blocks_added,
    }
    project["package_completeness"] = result
    intake["package_status"] = status
    project["intake"] = intake

    # Force not DOCUMENTS_READY when incomplete
    if not complete_flag and project.get("response_status") == "DOCUMENTS_READY":
        project["response_status"] = "DOCUMENTS_INCOMPLETE"

    return result
