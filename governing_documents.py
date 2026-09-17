"""Solicitation package map + governing document authority.

Reuses package_manifest / solicitation_package vocabulary; adds supersession
and active-requirement resolution for bid compliance intelligence.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from bid_compliance_constants import (
    AUTH_GOVERNING,
    AUTH_NON_GOVERNING,
    AUTH_REFERENCE,
    AUTH_UNKNOWN,
    DOC_AMENDMENT,
    DOC_HISTORICAL,
    DOC_QA,
    DOC_UNKNOWN,
    GOVERNING_DOC_TYPES,
    REFERENCE_ONLY_DOC_TYPES,
    REQ_ACTIVE,
    REQ_CONFLICTING,
    REQ_HISTORICAL,
    REQ_SUPERSEDED,
    REQ_UNRESOLVED_AUTH,
    REQUIREMENT_CONFLICT,
)
from package_manifest import manifest_entry


def _doc_id(filename: str | None = None, url: str | None = None, content: str | None = None) -> str:
    raw = f"{filename or ''}|{url or ''}|{(content or '')[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def governing_document(
    *,
    solicitation_id: str,
    filename: str | None = None,
    document_type: str = DOC_UNKNOWN,
    source_url: str | None = None,
    authoritative_source: str | None = None,
    publication_date: str | None = None,
    revision: str | None = None,
    amendment_number: str | None = None,
    effective_date: str | None = None,
    supersedes: str | None = None,
    superseded_by: str | None = None,
    governing: bool | None = None,
    attachment_type: str | None = None,
    checksum: str | None = None,
    text_available: bool = False,
    ocr_required: bool = False,
    provenance: str | None = None,
    confidence: str = "UNKNOWN",
    extracted_text: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    dtype = str(document_type or DOC_UNKNOWN).upper()
    if governing is None:
        if dtype in REFERENCE_ONLY_DOC_TYPES or dtype == DOC_HISTORICAL:
            authority = AUTH_REFERENCE
            is_governing = False
        elif dtype in GOVERNING_DOC_TYPES:
            authority = AUTH_GOVERNING
            is_governing = True
        else:
            authority = AUTH_UNKNOWN
            is_governing = False
    else:
        is_governing = bool(governing)
        authority = AUTH_GOVERNING if is_governing else AUTH_NON_GOVERNING
        if dtype in REFERENCE_ONLY_DOC_TYPES:
            authority = AUTH_REFERENCE
            is_governing = False

    did = document_id or _doc_id(filename, source_url, extracted_text)
    fp = checksum or (
        hashlib.sha256((extracted_text or filename or did).encode()).hexdigest()[:32]
        if (extracted_text or filename)
        else None
    )
    return {
        "kind": "GoverningDocument",
        "solicitation_id": solicitation_id,
        "document_id": did,
        "filename": filename,
        "document_type": dtype,
        "source_url": source_url,
        "acquisition_timestamp": now_utc().isoformat(),
        "authoritative_source": authoritative_source,
        "publication_date": publication_date,
        "revision": revision,
        "amendment_number": amendment_number,
        "effective_date": effective_date,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "governing": is_governing,
        "authority": authority,
        "attachment_type": attachment_type,
        "checksum": fp,
        "extracted_text_available": bool(text_available or extracted_text),
        "ocr_required": bool(ocr_required),
        "provenance": provenance or authoritative_source,
        "confidence": confidence,
        # Do not persist giant bodies; allow caller to pass snippet separately
        "text_snippet": (extracted_text or "")[:400] if extracted_text else None,
    }


def build_package_map(
    *,
    solicitation_id: str,
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize documents into SolicitationPackageMap with authority links."""
    docs = [dict(d) for d in (documents or [])]
    by_id = {d["document_id"]: d for d in docs if d.get("document_id")}

    # Apply explicit supersedes/superseded_by
    for d in docs:
        sid = d.get("supersedes")
        if sid and sid in by_id:
            by_id[sid]["superseded_by"] = d["document_id"]
            by_id[sid]["governing"] = False
            if by_id[sid].get("authority") == AUTH_GOVERNING:
                by_id[sid]["authority"] = AUTH_NON_GOVERNING

    governing = [d for d in docs if d.get("governing") and not d.get("superseded_by")]
    reference = [d for d in docs if d.get("authority") == AUTH_REFERENCE or d.get("document_type") in REFERENCE_ONLY_DOC_TYPES]
    amendments = [d for d in docs if d.get("document_type") == DOC_AMENDMENT]
    qa = [d for d in docs if d.get("document_type") == DOC_QA]
    superseded = [d for d in docs if d.get("superseded_by")]
    unresolved_auth = [d for d in docs if d.get("authority") == AUTH_UNKNOWN]

    # Also emit package_manifest-compatible entries for reuse
    manifest = [
        manifest_entry(
            document_type=str(d.get("document_type") or DOC_UNKNOWN),
            title=d.get("filename"),
            filename=d.get("filename"),
            source_url=d.get("source_url"),
            source_system=d.get("authoritative_source"),
            document_identifier=d.get("document_id"),
            version=d.get("revision"),
            amendment_number=d.get("amendment_number"),
            publication_date=d.get("publication_date"),
            content_hash=d.get("checksum"),
            superseded_by=d.get("superseded_by"),
            current_version=not bool(d.get("superseded_by")),
            required_for_package_completeness=bool(d.get("governing")),
            retrieval_status="KNOWN_RETRIEVED" if d.get("extracted_text_available") else "UNKNOWN",
            provenance=d.get("provenance"),
        )
        for d in docs
    ]

    return {
        "kind": "SolicitationPackageMap",
        "solicitation_id": solicitation_id,
        "documents": docs,
        "governing_documents": governing,
        "reference_only": reference,
        "amendments": amendments,
        "q_and_a": qa,
        "superseded_documents": superseded,
        "unresolved_authority": unresolved_auth,
        "manifest": manifest,
        "document_count": len(docs),
        "governing_count": len(governing),
        "can_distinguish_governing_vs_reference": True,
    }


def resolve_requirement_authority(
    *,
    field: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Resolve which value is ACTIVE for a requirement field.

    candidates: [{value, document_id, amendment_number, effective_date, authority}]
    Newer amendment_number / effective_date wins when authority is governing.
    Conflicts with equal authority → REQUIREMENT_CONFLICT_REQUIRES_RESOLUTION.
    """
    if not candidates:
        return {
            "field": field,
            "state": REQ_UNRESOLVED_AUTH,
            "value": None,
            "reason": "no_candidates",
        }

    gov = [c for c in candidates if c.get("authority") in (None, AUTH_GOVERNING) or c.get("governing")]
    pool = gov or candidates

    def _rank(c: dict[str, Any]) -> tuple:
        am = c.get("amendment_number")
        try:
            am_n = int(str(am).lstrip("0") or "0") if am is not None else -1
        except ValueError:
            am_n = -1
        return (am_n, str(c.get("effective_date") or ""))

    ranked = sorted(pool, key=lambda c: (_rank(c), str(c.get("document_id") or "")), reverse=True)
    winner = ranked[0]
    same_rank = [c for c in ranked if _rank(c) == _rank(winner)]
    distinct_values = {c.get("value") for c in same_rank}
    if len(distinct_values) > 1:
        return {
            "field": field,
            "state": REQ_CONFLICTING,
            "conflict": REQUIREMENT_CONFLICT,
            "value": None,
            "candidates": ranked,
            "reason": "equal_authority_conflicting_values",
            "active": False,
        }

    superseded_vals = []
    for c in ranked[1:]:
        if c.get("value") != winner.get("value"):
            superseded_vals.append(
                {
                    **c,
                    "state": REQ_SUPERSEDED,
                }
            )

    return {
        "field": field,
        "state": REQ_ACTIVE,
        "value": winner.get("value"),
        "source_document_id": winner.get("document_id"),
        "amendment_number": winner.get("amendment_number"),
        "superseded": superseded_vals,
        "historical": [
            {**c, "state": REQ_HISTORICAL} for c in candidates if c not in ranked
        ],
        "active": True,
    }


def apply_amendment_supersession(
    package_map: dict[str, Any],
    *,
    amendment_doc: dict[str, Any],
    supersedes_document_id: str | None = None,
) -> dict[str, Any]:
    """Mark prior document superseded by amendment; do not advance silently."""
    docs = list(package_map.get("documents") or [])
    amd = dict(amendment_doc)
    amd["document_type"] = DOC_AMENDMENT
    amd["governing"] = True
    amd["authority"] = AUTH_GOVERNING
    if not amd.get("document_id"):
        amd["document_id"] = f"amd-{uuid4().hex[:10]}"

    target = supersedes_document_id or amd.get("supersedes")
    for d in docs:
        if target and d.get("document_id") == target:
            d["superseded_by"] = amd["document_id"]
            d["governing"] = False
            amd["supersedes"] = target
        # If amendment_number increases vs base solicitation without explicit target,
        # still register amendment as governing
    docs.append(amd)
    sid = package_map.get("solicitation_id") or amd.get("solicitation_id") or "unknown"
    return build_package_map(solicitation_id=sid, documents=docs)
