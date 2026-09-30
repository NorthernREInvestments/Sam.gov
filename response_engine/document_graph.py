"""Controlling document graph — base, amendments, supersession, conflicts."""

from __future__ import annotations

from typing import Any

from response_engine.constants import (
    ADDENDUM,
    AMENDMENT,
    CONTROLLING,
    MATERIAL,
    PACKAGE_STALE_DUE_TO_AMENDMENT,
    POTENTIALLY_MATERIAL,
    SUPERSEDED_DOC,
    UNKNOWN_MATERIALITY,
)
from response_engine.models import new_amendment, new_clarification


def find_duplicate(documents: list[dict[str, Any]], content_hash: str) -> dict[str, Any] | None:
    if not content_hash:
        return None
    for d in documents:
        if d.get("content_hash") == content_hash or d.get("file_hash") == content_hash:
            return d
    return None


def add_document_to_graph(
    project: dict[str, Any],
    document: dict[str, Any],
    *,
    supersedes_document_id: str | None = None,
) -> dict[str, Any]:
    """Add document idempotently by content hash; update supersession edges."""
    docs = project.setdefault("documents", [])
    dup = find_duplicate(docs, document.get("content_hash") or "")
    if dup:
        # provenance only — do not store byte-identical copy
        refs = dup.setdefault("provenance_refs", [])
        refs.append(
            {
                "source_url": document.get("source_url"),
                "filename": document.get("filename"),
                "retrieved_at": document.get("retrieved_at"),
            }
        )
        return dup

    if supersedes_document_id:
        document["supersedes_document_id"] = supersedes_document_id
        for d in docs:
            if d.get("document_id") == supersedes_document_id:
                d["superseded_by_document_id"] = document["document_id"]
                d["controlling_status"] = SUPERSEDED_DOC
                project.setdefault("document_graph", {}).setdefault("edges", []).append(
                    {
                        "from": supersedes_document_id,
                        "to": document["document_id"],
                        "relation": "SUPERSEDED_BY",
                    }
                )
                break

    if document.get("document_type") in {AMENDMENT, ADDENDUM}:
        for d in docs:
            if d.get("document_type") not in {AMENDMENT, ADDENDUM} and d.get("controlling_status") == CONTROLLING:
                project.setdefault("document_graph", {}).setdefault("edges", []).append(
                    {
                        "from": document["document_id"],
                        "to": d["document_id"],
                        "relation": "MODIFIES",
                    }
                )

    docs.append(document)
    project["current_controlling_version"] = _controlling_version_label(project)
    return document


def register_amendment(
    project: dict[str, Any],
    *,
    number: str,
    document_id: str | None = None,
    issued_at: str | None = None,
    changes: dict[str, bool] | None = None,
    materiality: str | None = None,
) -> dict[str, Any]:
    changes = changes or {}
    # Default toward material if any material flag set or unknown
    mat = materiality
    if mat is None:
        if any(changes.values()):
            mat = MATERIAL if any(
                changes.get(k) for k in ("deadline", "quantity", "price", "specification", "delivery", "submission", "documents", "forms", "evaluation")
            ) else POTENTIALLY_MATERIAL
        else:
            mat = POTENTIALLY_MATERIAL  # never assume harmless
    # Idempotent by amendment number
    for a in project.get("amendments") or []:
        if str(a.get("number")) == str(number):
            return a
    amd = new_amendment(
        number=number,
        issued_at=issued_at,
        document_id=document_id,
        materiality=mat,
        changes=changes,
        acknowledgment_required=True,
    )
    project.setdefault("amendments", []).append(amd)
    project["package_status"] = PACKAGE_STALE_DUE_TO_AMENDMENT
    project.setdefault("history", []).append(
        {"at": amd.get("issued_at"), "event": "amendment_registered", "number": number, "materiality": mat}
    )
    return amd


def controlling_documents(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in project.get("documents") or [] if d.get("controlling_status") == CONTROLLING]


def superseded_documents(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in project.get("documents") or [] if d.get("controlling_status") == SUPERSEDED_DOC]


def detect_document_conflicts(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Flag conflicts when two CONTROLLING docs disagree on delivery/deadline without supersession."""
    conflicts: list[dict[str, Any]] = []
    controlling = controlling_documents(project)
    # Simple: two amendments both CONTROLLING with same field changes and no edge
    amds = [d for d in controlling if d.get("document_type") in {AMENDMENT, ADDENDUM}]
    if len(amds) >= 2:
        # Check for delivery date excerpts that differ
        texts = [(d["document_id"], (d.get("text") or "")) for d in amds]
        import re
        dates = []
        for did, text in texts:
            m = re.search(r"deliver(?:y|ed)?[^\n]{0,40}?(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+ \d{1,2},? \d{4})", text, re.I)
            if m:
                dates.append((did, m.group(1)))
        if len(dates) >= 2 and dates[0][1] != dates[1][1]:
            conflict = {
                "kind": "DOCUMENT_CONFLICT",
                "field": "delivery_date",
                "document_ids": [dates[0][0], dates[1][0]],
                "values": [dates[0][1], dates[1][1]],
            }
            conflicts.append(conflict)
            project.setdefault("document_graph", {}).setdefault("conflicts", []).append(conflict)
            project.setdefault("clarifications", []).append(
                new_clarification(
                    issue=f"Controlling documents conflict on delivery date: {dates[0][1]} vs {dates[1][1]}",
                    sources=[dates[0][0], dates[1][0]],
                    materiality=MATERIAL,
                    recommended_question="Which delivery date governs — please confirm the controlling amendment.",
                )
            )
    return conflicts


def invalidate_requirements_for_amendment(
    project: dict[str, Any],
    *,
    category_hints: set[str] | None = None,
    amendment_number: str | None = None,
) -> list[str]:
    """Mark matching active requirements SUPERSEDED (history retained). Returns superseded ids."""
    superseded_ids: list[str] = []
    hints = category_hints or set()
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        cat = req.get("requirement_category")
        if hints and cat not in hints:
            continue
        if not hints:
            # If no hints, only supersede when amendment says quantity/spec/deadline categories
            continue
        req["superseded"] = True
        req["answer_status"] = "SUPERSEDED"
        req["compliance_status"] = "SUPERSEDED"
        req["notes"] = (req.get("notes") or "") + f" Superseded by amendment {amendment_number or ''}."
        superseded_ids.append(req["requirement_id"])
    project["package_status"] = PACKAGE_STALE_DUE_TO_AMENDMENT
    return superseded_ids


def _controlling_version_label(project: dict[str, Any]) -> str:
    amds = project.get("amendments") or []
    if amds:
        last = sorted(amds, key=lambda a: str(a.get("number") or ""))[-1]
        return f"Amendment {last.get('number')}"
    bases = [d for d in project.get("documents") or [] if d.get("document_type") == "BASE_SOLICITATION"]
    if bases:
        return bases[0].get("version") or bases[0].get("title") or "BASE"
    return "UNKNOWN"
