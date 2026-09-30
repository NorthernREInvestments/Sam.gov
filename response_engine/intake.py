"""Solicitation intake — document set ingest with hash dedupe."""

from __future__ import annotations

import re
from typing import Any

from response_engine.constants import (
    ADDENDUM,
    AMENDMENT,
    BASE_SOLICITATION,
    BUYER_TEMPLATE,
    CERTIFICATION,
    CONTROLLING,
    FORM,
    INTAKE_IN_PROGRESS,
    MASTER_SOLICITATION,
    PORTAL_INSTRUCTIONS,
    PRICING_SHEET,
    Q_AND_A,
    SPECIFICATION,
    STATEMENT_OF_WORK,
)
from response_engine.document_graph import add_document_to_graph, register_amendment
from response_engine.models import content_hash, new_document


def classify_document_type(
    *,
    title: str | None = None,
    filename: str | None = None,
    text: str | None = None,
    hinted_type: str | None = None,
) -> str:
    if hinted_type:
        return str(hinted_type).upper()
    blob = f"{title or ''} {filename or ''} {(text or '')[:2000]}".lower()
    if re.search(r"\bamendment\b|\bamend\s*0*\d+\b", blob):
        return AMENDMENT
    if re.search(r"\baddendum\b", blob):
        return ADDENDUM
    if re.search(r"\bq\s*&\s*a\b|\bquestions?\s+and\s+answers?\b", blob):
        return Q_AND_A
    if re.search(r"\bpricing\s+(?:sheet|schedule)|cost\s+sheet|\.xlsx?\b", blob):
        return PRICING_SHEET
    if re.search(r"\bbuyer\s+template\b|\bprice\s+schedule\s+template\b", blob):
        return BUYER_TEMPLATE
    if re.search(r"\bsf\s*1449\b|\bsf\s*33\b|certification\s+form", blob):
        return FORM if "cert" not in blob else CERTIFICATION
    if re.search(r"\bstatement\s+of\s+work\b|\bsow\b", blob):
        return STATEMENT_OF_WORK
    if re.search(r"\bspecification\b|\bsalient\b", blob):
        return SPECIFICATION
    if re.search(r"\bmaster\s+solicitation\b", blob):
        return MASTER_SOLICITATION
    if re.search(r"\bportal\s+instruction|\bsubmission\s+instruction", blob):
        return PORTAL_INSTRUCTIONS
    if re.search(r"\brfq\b|\brfp\b|\bifb\b|\bsolicitation\b|\binvitation\b", blob):
        return BASE_SOLICITATION
    return "OTHER"


def ingest_document(
    project: dict[str, Any],
    *,
    title: str | None = None,
    filename: str | None = None,
    source_url: str | None = None,
    text: str | None = None,
    document_type: str | None = None,
    authoritative_source: str | None = None,
    source_system: str | None = None,
    version: str | None = None,
    amendment_number: str | None = None,
    supersedes_document_id: str | None = None,
    is_buyer_template: bool = False,
    published_at: str | None = None,
) -> dict[str, Any]:
    project["response_status"] = INTAKE_IN_PROGRESS
    dtype = classify_document_type(title=title, filename=filename, text=text, hinted_type=document_type)
    if dtype in {PRICING_SHEET, BUYER_TEMPLATE} or (filename or "").lower().endswith((".xlsx", ".xls", ".csv")):
        is_buyer_template = True
        if dtype == "OTHER":
            dtype = BUYER_TEMPLATE

    # Amendment number from title
    if dtype == AMENDMENT and not amendment_number:
        m = re.search(r"amendment\s*(?:no\.?|#)?\s*0*(\d+)", f"{title or ''} {filename or ''}", re.I)
        if m:
            amendment_number = m.group(1)

    doc = new_document(
        response_project_id=project["response_project_id"],
        document_type=dtype,
        title=title,
        filename=filename,
        source_url=source_url,
        authoritative_source=authoritative_source or project.get("authoritative_source"),
        source_system=source_system or project.get("discovery_source"),
        text=text,
        version=version,
        amendment_number=amendment_number,
        published_at=published_at,
        controlling_status=CONTROLLING,
        is_buyer_template=is_buyer_template,
    )
    stored = add_document_to_graph(project, doc, supersedes_document_id=supersedes_document_id)

    if dtype == AMENDMENT and amendment_number:
        changes = _infer_amendment_changes(text or "")
        register_amendment(
            project,
            number=amendment_number,
            document_id=stored["document_id"],
            issued_at=published_at,
            changes=changes,
        )
    return stored


def ingest_document_set(project: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Ingest many docs; skip byte-identical duplicates."""
    results = {"ingested": 0, "duplicates": 0, "documents": []}
    seen_hashes: set[str] = {d.get("content_hash") for d in project.get("documents") or [] if d.get("content_hash")}
    for raw in documents:
        text = raw.get("text")
        ch = content_hash(text) if text else content_hash(f"{raw.get('filename')}|{raw.get('source_url')}|{raw.get('title')}")
        if ch and ch in seen_hashes:
            results["duplicates"] += 1
            continue
        doc = ingest_document(project, **{k: raw.get(k) for k in (
            "title", "filename", "source_url", "text", "document_type",
            "authoritative_source", "source_system", "version", "amendment_number",
            "supersedes_document_id", "is_buyer_template", "published_at",
        ) if raw.get(k) is not None})
        if ch:
            seen_hashes.add(ch)
        results["ingested"] += 1
        results["documents"].append(doc["document_id"])
    return results


def _infer_amendment_changes(text: str) -> dict[str, bool]:
    t = (text or "").lower()
    return {
        "deadline": bool(re.search(r"deadline|due date|closing", t)),
        "quantity": bool(re.search(r"quantity|qty", t)),
        "price": bool(re.search(r"price|pricing|clin", t)),
        "specification": bool(re.search(r"specification|salient|model|part\s*number", t)),
        "delivery": bool(re.search(r"deliver", t)),
        "evaluation": bool(re.search(r"evaluation|lpta|best value", t)),
        "submission": bool(re.search(r"submit|portal|piee|dibbs", t)),
        "documents": bool(re.search(r"attach|form|document required", t)),
        "forms": bool(re.search(r"sf\s*1449|sf\s*33|form", t)),
        "other": False,
    }
