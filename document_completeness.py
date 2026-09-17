"""Evidence-driven solicitation package completeness evaluation."""

from __future__ import annotations

from typing import Any

from package_manifest import (
    KNOWN_RETRIEVED,
    PACKAGE_COMPLETE,
    PACKAGE_INCOMPLETE,
    PACKAGE_UNRESOLVED,
    REFERENCED_NOT_RETRIEVED,
    RETRIEVAL_FAILED,
    SUPERSEDED,
    manifest_entry,
)


def evaluate_package_completeness(
    *,
    manifest: list[dict[str, Any]],
    references: list[dict[str, Any]] | None = None,
    required_external_refs: list[dict[str, Any]] | None = None,
    amendments_expected: bool | None = None,
    amendments_accounted: bool | None = None,
    qa_expected: bool | None = None,
    qa_accounted: bool | None = None,
    source_conflicts: list[str] | None = None,
) -> dict[str, Any]:
    """
    COMPLETE only when required referenced docs accounted for and base RFQ present.
    One downloaded PDF alone is never enough unless reference analysis shows no
    required external gaps AND checklist is resolved.
    """
    items = [m for m in (manifest or []) if isinstance(m, dict)]
    current = [
        m
        for m in items
        if m.get("current_version", True)
        and m.get("retrieval_status") != SUPERSEDED
        and not m.get("superseded_by")
    ]
    retrieved = [m for m in current if m.get("retrieval_status") == KNOWN_RETRIEVED]
    missing_required = [
        m
        for m in current
        if m.get("required_for_package_completeness")
        and m.get("retrieval_status") in {REFERENCED_NOT_RETRIEVED, RETRIEVAL_FAILED, "KNOWN_NOT_RETRIEVED"}
    ]
    possible = [m for m in current if m.get("retrieval_status") == "POSSIBLE_DOCUMENT"]

    reasoning: list[str] = []
    blockers: list[str] = []

    has_rfq = any(
        str(m.get("document_type") or "").upper() in {"RFQ", "RFP", "IFB", "BASE_SOLICITATION"}
        and m.get("retrieval_status") == KNOWN_RETRIEVED
        for m in retrieved
    )
    if not has_rfq:
        # also accept rfq_rfp_ifb lowercase legacy
        has_rfq = any(
            str(m.get("document_type") or "").lower() in {"rfq", "rfp", "ifb", "rfq_rfp_ifb", "base_solicitation"}
            and m.get("retrieval_status") == KNOWN_RETRIEVED
            for m in retrieved
        )
    if not has_rfq:
        blockers.append("base_solicitation_or_rfq_not_retrieved")
        reasoning.append("No retrieved base RFQ/RFP/IFB in manifest.")

    if len(retrieved) == 1 and not required_external_refs and amendments_accounted is None:
        blockers.append("single_retrieved_document_checklist_unresolved")
        reasoning.append("One retrieved file alone cannot prove completeness without resolved checklist.")

    for m in missing_required:
        blockers.append(
            f"required_document_not_retrieved:{m.get('title') or m.get('filename') or m.get('document_type')}"
        )
        reasoning.append(
            f"Missing required: {m.get('title') or m.get('filename')} "
            f"(referenced_by={m.get('referenced_by')})"
        )

    # Explicit required external refs without matching retrieved file
    for ref in required_external_refs or []:
        label = ref.get("matched_text") or ref.get("capture")
        matched = False
        for m in retrieved:
            fn = (m.get("filename") or "") + (m.get("title") or "")
            if label and str(label).lower().replace(" ", "") in fn.lower().replace(" ", ""):
                matched = True
                break
        if not matched and ref.get("required_for_package_completeness"):
            blockers.append(f"referenced_required_unretrieved:{label}")
            reasoning.append(f"Referenced required document not retrieved: {label}")

    if amendments_expected is True and amendments_accounted is not True:
        blockers.append("amendments_unaccounted")
        reasoning.append("Amendments expected but not accounted for.")
    if qa_expected is True and qa_accounted is not True:
        blockers.append("qa_unaccounted")
        reasoning.append("Q&A expected but not accounted for.")

    if source_conflicts:
        blockers.append("source_conflict")
        reasoning.extend([f"conflict:{c}" for c in source_conflicts])

    # Possible docs do not equal known missing
    if possible and not missing_required:
        reasoning.append(
            f"{len(possible)} POSSIBLE_DOCUMENT entries — not treated as known missing."
        )

    # Can complete when: RFQ retrieved, no missing required, amendments/qa resolved, no conflicts
    checklist_ok = (
        amendments_accounted is not None
        and (qa_accounted is not None or qa_expected is False or qa_expected is None)
    )
    # If no external required refs and RFQ has in-document sections only
    no_external_gaps = not missing_required and not any(
        b.startswith("referenced_required_unretrieved") for b in blockers
    )

    if has_rfq and no_external_gaps and checklist_ok and not source_conflicts:
        # Clear soft single-doc blocker when checklist explicitly resolved
        blockers = [b for b in blockers if b != "single_retrieved_document_checklist_unresolved"]
        if not blockers:
            reasoning.append(
                "Base RFQ retrieved; no required external references unretrieved; "
                "amendment/Q&A accounting resolved."
            )
            return {
                "status": PACKAGE_COMPLETE,
                "blockers": [],
                "reasoning": reasoning,
                "retrieved_count": len(retrieved),
                "missing_required": [],
                "possible_missing": possible,
                "manifest_count": len(items),
            }

    if missing_required or any(b.startswith("referenced_required") for b in blockers):
        status = PACKAGE_INCOMPLETE
    elif not has_rfq:
        status = PACKAGE_INCOMPLETE
    else:
        status = PACKAGE_UNRESOLVED
        if not checklist_ok:
            reasoning.append(
                "Completeness unresolved: amendment/Q&A accounting or required-reference "
                "checklist not fully resolved."
            )

    return {
        "status": status,
        "blockers": blockers,
        "reasoning": reasoning,
        "retrieved_count": len(retrieved),
        "missing_required": missing_required,
        "possible_missing": possible,
        "manifest_count": len(items),
        "specific_next_actions": _specific_missing_actions(missing_required, blockers, reasoning),
    }


def _specific_missing_actions(
    missing_required: list[dict[str, Any]],
    blockers: list[str],
    reasoning: list[str],
) -> list[str]:
    actions = []
    for m in missing_required:
        actions.append(
            f"Missing: {m.get('title') or m.get('filename') or m.get('document_type')}. "
            f"Referenced by: {m.get('referenced_by') or 'unknown'}. "
            f"Retrieval: {m.get('retrieval_status')}. "
            f"Next: locate/retrieve this document without SAM if public URL known."
        )
    if not actions and blockers:
        actions.append(
            "Package completeness unresolved: "
            + "; ".join(reasoning[:3] if reasoning else blockers[:3])
        )
    return actions


def supersede_document(
    prior: dict[str, Any],
    *,
    successor_id: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preserve prior evidence; mark superseded. Never silently overwrite."""
    old = dict(prior)
    old["current_version"] = False
    old["retrieval_status"] = SUPERSEDED
    old["superseded_by"] = successor_id
    old["evidence"] = (old.get("evidence") or "") + f" | superseded:{reason}"
    return old, prior
