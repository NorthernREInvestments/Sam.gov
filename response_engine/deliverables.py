"""Submission deliverable inventory — classify required response artifacts."""

from __future__ import annotations

from typing import Any

from response_engine.constants import (
    AMENDMENT_ACKNOWLEDGMENT,
    AT_SUBMISSION,
    BUYER_TEMPLATE_DELIVERABLE,
    CLIN_PRICE,
    DOCUMENT_UPLOAD,
    EMAIL_ATTACHMENT,
    FORM_DELIVERABLE,
    PHYSICAL_PACKAGE,
    PHYSICAL_SAMPLE,
    PORTAL_FIELD,
    SIGNATURE,
    BID_GUARANTEE,
)
from response_engine.models import new_deliverable

_CATEGORY_TO_DELIVERABLE = {
    "FORM": FORM_DELIVERABLE,
    "SIGNATURE": SIGNATURE,
    "AMENDMENT_ACK": AMENDMENT_ACKNOWLEDGMENT,
    "ATTACHMENT": DOCUMENT_UPLOAD,
    "TECHNICAL_LITERATURE": DOCUMENT_UPLOAD,
    "CERTIFICATION": FORM_DELIVERABLE,
    "PRICE": CLIN_PRICE,
    "PRICING": CLIN_PRICE,
    "PHYSICAL_SAMPLE": PHYSICAL_SAMPLE,
    "BID_GUARANTEE": BID_GUARANTEE,
    "SUBMISSION_METHOD": PORTAL_FIELD,
    "FILE_FORMAT": DOCUMENT_UPLOAD,
    "FILE_NAME": DOCUMENT_UPLOAD,
    "PAGE_LIMIT": DOCUMENT_UPLOAD,
}


def inventory_deliverables(project: dict[str, Any]) -> list[dict[str, Any]]:
    existing = {(d.get("type"), d.get("source_requirement_id")) for d in project.get("deliverables") or []}
    out = list(project.get("deliverables") or [])

    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        cat = req.get("requirement_category")
        dtype = _CATEGORY_TO_DELIVERABLE.get(cat)
        if not dtype:
            continue
        key = (dtype, req["requirement_id"])
        if key in existing:
            continue
        existing.add(key)
        out.append(
            new_deliverable(
                dtype=dtype,
                required=bool(req.get("mandatory")),
                source_requirement_id=req["requirement_id"],
                due_timing=req.get("timing") or AT_SUBMISSION,
                format_hint=None,
                signature_requirement=cat == "SIGNATURE",
                upload_destination=project.get("submission_system"),
                label=f"{cat}: {(req.get('normalized_requirement') or '')[:60]}",
            )
        )

    # Buyer templates
    for doc in project.get("documents") or []:
        if doc.get("is_buyer_template") or doc.get("document_type") in {"BUYER_TEMPLATE", "PRICING_SHEET", "COST_SHEET"}:
            key = (BUYER_TEMPLATE_DELIVERABLE, doc["document_id"])
            if key in existing:
                continue
            existing.add(key)
            out.append(
                new_deliverable(
                    dtype=BUYER_TEMPLATE_DELIVERABLE,
                    required=True,
                    source_requirement_id=None,
                    due_timing=AT_SUBMISSION,
                    format_hint=doc.get("filename"),
                    filename_requirement=doc.get("filename"),
                    upload_destination=project.get("submission_system"),
                    label=f"Buyer template: {doc.get('title') or doc.get('filename')}",
                )
            )

    if (project.get("submission_system") or "").upper() == "EMAIL":
        key = (EMAIL_ATTACHMENT, None)
        if key not in existing:
            out.append(new_deliverable(dtype=EMAIL_ATTACHMENT, required=True, label="Email attachments per instructions"))

    if (project.get("response_type") or "") == "PHYSICAL_BID":
        out.append(new_deliverable(dtype=PHYSICAL_PACKAGE, required=True, label="Physical bid package"))

    project["deliverables"] = out
    return out


def deliverable_counts_by_type(project: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in project.get("deliverables") or []:
        t = d.get("type") or "OTHER"
        counts[t] = counts.get(t, 0) + 1
    return counts
