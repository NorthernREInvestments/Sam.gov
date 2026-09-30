"""R1 service — create projects, compile, persist, UI read models."""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

from response_engine.classifier import (
    classify_evaluation_method,
    classify_response_type,
    detect_federal_sections,
    detect_product_mode,
)
from response_engine.compliance import (
    build_compliance_matrix,
    compute_hard_blocks,
    evaluate_exact_brand_mismatch,
    evaluate_salient_generic_equal_claim,
    operator_compliance_summary,
    set_requirement_answer,
)
from response_engine.constants import (
    BUILD,
    DOCUMENTS_READY,
    REQUIREMENTS_COMPILING,
)
from response_engine.deliverables import deliverable_counts_by_type, inventory_deliverables
from response_engine.document_graph import (
    detect_document_conflicts,
    invalidate_requirements_for_amendment,
)
from response_engine.firewall import firewall_report, ingest_supplier_quote_as_internal
from response_engine.intake import ingest_document, ingest_document_set
from response_engine.models import new_requirement, new_response_project, empty_provenance
from response_engine.requirements import compile_requirements_from_text, merge_requirements
from response_engine.store import find_by_opportunity, load_project, save_project


def _utc() -> str:
    return now_utc().isoformat()


def create_or_get_project_from_opportunity(
    *,
    canonical_opportunity_id: str,
    buyer: str | None = None,
    solicitation_number: str | None = None,
    title: str | None = None,
    jurisdiction: str | None = None,
    discovery_source: str | None = None,
    authoritative_source: str | None = None,
    submission_system: str | None = None,
    force_new: bool = False,
) -> dict[str, Any]:
    if not force_new:
        existing = find_by_opportunity(canonical_opportunity_id)
        if existing:
            return existing
    project = new_response_project(
        canonical_opportunity_id=canonical_opportunity_id,
        buyer=buyer,
        solicitation_number=solicitation_number,
        title=title,
        jurisdiction=jurisdiction,
        discovery_source=discovery_source,
        authoritative_source=authoritative_source,
        submission_system=submission_system,
    )
    return save_project(project)


def compile_project(project: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Full R1 compilation pass over ingested documents."""
    project["response_status"] = REQUIREMENTS_COMPILING
    texts = []
    for doc in project.get("documents") or []:
        if doc.get("controlling_status") == "SUPERSEDED":
            continue
        texts.append(doc.get("text") or "")
    combined = "\n\n".join(t for t in texts if t)

    # Classification
    rt = classify_response_type(
        text=combined,
        jurisdiction=project.get("jurisdiction"),
        discovery_source=project.get("discovery_source"),
        authoritative_source=project.get("authoritative_source"),
        submission_system=project.get("submission_system"),
    )
    project["response_type"] = rt["result"]
    project["response_type_evidence"] = rt["evidence"]
    if rt.get("owner_review_required"):
        project["owner_review_required"] = True

    ev = classify_evaluation_method(text=combined)
    project["evaluation_method"] = ev["methods"]
    project["evaluation_evidence"] = ev["evidence"]

    project["federal_sections_present"] = detect_federal_sections(combined)
    pm = detect_product_mode(combined)
    project["product_mode"] = pm["mode"]
    project["product_mode_evidence"] = pm

    # Deadlines
    _extract_deadlines(project, combined)

    # Requirements from each controlling document
    existing_keys: set[str] = set()
    for req in project.get("requirements") or []:
        if not req.get("superseded"):
            existing_keys.add(
                f"{req.get('requirement_category')}|{(req.get('requirement_text') or '')[:120]}|{req.get('source_document_id')}"
            )

    for doc in project.get("documents") or []:
        if doc.get("controlling_status") == "SUPERSEDED":
            continue
        text = doc.get("text") or ""
        if not text.strip():
            continue
        new_reqs = compile_requirements_from_text(
            response_project_id=project["response_project_id"],
            text=text,
            source_document_id=doc["document_id"],
            amendment_version=doc.get("amendment_number"),
            existing_keys=existing_keys,
        )
        merge_requirements(project, new_reqs)

    # Amendment acknowledgment requirement when amendments exist
    if project.get("amendments"):
        _ensure_amendment_ack_requirement(project)

    detect_document_conflicts(project)
    inventory_deliverables(project)
    build_compliance_matrix(project)
    compute_hard_blocks(project)

    if project.get("documents"):
        if project["response_status"] == REQUIREMENTS_COMPILING:
            # compute_hard_blocks sets status; if none, mark docs ready path
            pass
    else:
        project["response_status"] = "DOCUMENTS_INCOMPLETE"

    project.setdefault("history", []).append({"at": _utc(), "event": "compiled", "build": BUILD})
    if persist:
        save_project(project)
    # R2 CLIN/pricing/technical pass (canonical economics path; never fabricates quotes)
    try:
        from response_engine.r2_service import run_r2_analysis

        run_r2_analysis(project, persist=persist)
    except Exception as exc:
        project.setdefault("r2_errors", []).append(str(exc)[:300])
        if persist:
            save_project(project)
    # R3 company/compliance pass (canonical eligibility path; never auto-certifies)
    try:
        from response_engine.r3_service import run_r3_analysis

        run_r3_analysis(project, persist=persist)
    except Exception as exc:
        project.setdefault("r3_errors", []).append(str(exc)[:300])
        if persist:
            save_project(project)
    return project


def apply_amendment_quantity_change(
    project: dict[str, Any],
    *,
    amendment_number: str,
    new_quantity: int,
    document_id: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Test/helper: supersede quantity requirements and add new current quantity."""
    from response_engine.document_graph import register_amendment

    if text and document_id is None:
        doc = ingest_document(
            project,
            title=f"Amendment {amendment_number}",
            text=text,
            document_type="AMENDMENT",
            amendment_number=amendment_number,
        )
        document_id = doc["document_id"]
    register_amendment(
        project,
        number=amendment_number,
        document_id=document_id,
        changes={"quantity": True},
        materiality="MATERIAL",
    )
    superseded = invalidate_requirements_for_amendment(
        project, category_hints={"QUANTITY"}, amendment_number=amendment_number
    )
    new_req = new_requirement(
        response_project_id=project["response_project_id"],
        requirement_text=f"Quantity = {new_quantity}",
        normalized_requirement=f"quantity:{new_quantity}",
        requirement_category="QUANTITY",
        source_document_id=document_id,
        mandatory=True,
        materiality="MATERIAL",
        provenance=empty_provenance(
            document_id=document_id,
            excerpt=f"Quantity = {new_quantity}",
            extractor="r1_amendment_diff",
            confidence="HIGH",
        ),
        amendment_version=str(amendment_number),
        confidence="HIGH",
    )
    for sid in superseded:
        for r in project.get("requirements") or []:
            if r["requirement_id"] == sid:
                r["superseded_by_requirement_id"] = new_req["requirement_id"]
    merge_requirements(project, [new_req])
    # Ack requirement
    _ensure_amendment_ack_requirement(project)
    build_compliance_matrix(project)
    compute_hard_blocks(project)
    save_project(project)
    try:
        from response_engine.r2_service import apply_quantity_amendment_to_r2

        apply_quantity_amendment_to_r2(project, line_item_id=None, new_qty=str(new_quantity))
    except Exception:
        pass
    return project


def get_project_view(response_project_id: str) -> dict[str, Any] | None:
    project = load_project(response_project_id)
    if not project:
        return None
    return {
        "kind": "ResponseProjectView",
        "build": BUILD,
        "project": _public_project(project),
        "operator_summary": operator_compliance_summary(project),
        "firewall": firewall_report(project),
        "deliverable_counts": deliverable_counts_by_type(project),
    }


def bid_prep_card_for_opportunity(canonical_opportunity_id: str, base_card: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enrich Owner UI Bid Prep card with R1 foundation status."""
    project = find_by_opportunity(canonical_opportunity_id)
    card = dict(base_card or {})
    if not project:
        card["r1"] = None
        card["primary_action"] = "START BID PREP"
        card["missing_items"] = ["Response project not started"]
        card["note"] = "Start Bid Prep to read the real solicitation package and compile requirements."
        return card
    summary = operator_compliance_summary(project)
    pc = project.get("package_completeness") or {}
    missing_refs = pc.get("references_missing") or project.get("intake_missing_refs") or []
    summary = {
        **summary,
        "authoritative_source": project.get("authoritative_source"),
        "package_status": pc.get("status"),
        "missing_refs": ", ".join(r.get("label") or str(r) for r in missing_refs[:5]) if missing_refs else None,
        "evaluation_method": project.get("evaluation_method") or [],
    }
    card["r1"] = summary
    card["response_project_id"] = project["response_project_id"]
    card["checklist"] = [
        {"id": "docs", "label": f"Documents ({summary['documents_loaded']} loaded)", "done": summary["documents_loaded"] > 0},
        {"id": "package", "label": f"Package {pc.get('status') or 'UNKNOWN'}", "done": bool(pc.get("document_package_complete"))},
        {"id": "reqs", "label": f"Requirements ({summary['requirements_found']} found)", "done": summary["requirements_found"] > 0},
        {"id": "compliance", "label": summary["plain"]["compliance"], "done": summary["hard_blockers"] == 0 and summary["material_unresolved"] == 0},
        {"id": "clarifications", "label": summary["plain"]["questions"], "done": summary["clarifications_open"] == 0},
        {"id": "blockers", "label": f"{summary['hard_blockers']} hard blockers", "done": summary["hard_blockers"] == 0},
    ]
    card["missing_items"] = []
    if missing_refs:
        card["missing_items"].append(f"{len(missing_refs)} missing referenced documents")
    if summary["hard_blockers"]:
        card["missing_items"].append(f"{summary['hard_blockers']} hard blockers")
    if summary["material_unresolved"]:
        card["missing_items"].append(f"{summary['material_unresolved']} material items need data")
    if summary["clarifications_open"]:
        card["missing_items"].append(f"{summary['clarifications_open']} clarifications open")
    card["primary_action"] = "OPEN BID PREP" if project.get("documents") else "START BID PREP"
    card["next_action"] = summary["next_action"]
    card["response_status"] = summary["status"]
    # R1.2 package health + review queues
    docs = project.get("documents") or []
    card["package_health"] = {
        "documents_found": len(docs),
        "documents_parsed": sum(1 for d in docs if d.get("text") or d.get("workbook") or d.get("parse_status") in {"FETCHED", "OCR_FETCHED"}),
        "amendments": len(project.get("amendments") or []),
        "buyer_templates": sum(1 for d in docs if d.get("is_buyer_template")),
        "missing_references": len(missing_refs),
        "status": pc.get("status"),
        "document_package_complete": pc.get("document_package_complete"),
    }
    card["ocr_review_queue"] = project.get("ocr_review_queue") or []
    card["amendment_review_queue"] = project.get("amendment_review_queue") or []
    if card["ocr_review_queue"]:
        card["missing_items"].append(f"{len(card['ocr_review_queue'])} scanned page(s) need OCR review")
        card["operator_status"] = "Scanned Page Needs Review"
    elif missing_refs:
        card["operator_status"] = "Missing Document"
    elif project.get("package_stale") or project.get("response_status") == "PACKAGE_STALE_DUE_TO_AMENDMENT":
        card["operator_status"] = "Amendment Changed Requirements"
    elif summary.get("clarifications_open"):
        card["operator_status"] = "Clarification Needed"
    elif summary.get("status") == "READY_FOR_RESPONSE_BUILD":
        card["operator_status"] = "Ready for Response Build"
    elif summary.get("requirements_found"):
        card["operator_status"] = "Requirements Compiled"
    elif docs:
        card["operator_status"] = "Solicitation Loaded"
    else:
        card["operator_status"] = "Start Bid Prep"
    # R2 technical / economics panel
    try:
        from response_engine.r2_service import r2_operator_card, run_r2_analysis

        if not project.get("r2_analyzed_at") and project.get("documents"):
            run_r2_analysis(project, persist=True)
            project = find_by_opportunity(canonical_opportunity_id) or project
        if project.get("r2_analyzed_at"):
            card["r2"] = r2_operator_card(project)
            card["checklist"].append(
                {
                    "id": "r2_lines",
                    "label": f"Lines ({(card['r2'].get('lines') or {}).get('count') or 0})",
                    "done": bool((card["r2"].get("lines") or {}).get("count")),
                }
            )
            card["checklist"].append(
                {
                    "id": "r2_tech",
                    "label": f"Technical fail={(card['r2'].get('technical') or {}).get('fail') or 0}",
                    "done": not (card["r2"].get("technical") or {}).get("hard_fail"),
                }
            )
            if card["r2"].get("blockers"):
                card["missing_items"].extend(card["r2"]["blockers"][:3])
            if card["r2"].get("next_action"):
                card["next_action"] = card["r2"]["next_action"]
    except Exception:
        card["r2"] = None
    # R3 company compliance panel
    try:
        from response_engine.r3_service import r3_operator_card, run_r3_analysis

        if not project.get("r3_analyzed_at") and project.get("documents"):
            run_r3_analysis(project, persist=True)
            project = find_by_opportunity(canonical_opportunity_id) or project
        if project.get("r3_analyzed_at"):
            card["r3"] = r3_operator_card(project)
            card["checklist"].append(
                {
                    "id": "r3_compliance",
                    "label": f"Company compliance: {card['r3'].get('readiness') or 'UNKNOWN'}",
                    "done": card["r3"].get("readiness")
                    in {"COMPLIANCE_READY", "READY_FOR_RESPONSE_BUILD", "REGISTRATION_REQUIRED"},
                }
            )
            if card["r3"].get("blockers"):
                card["missing_items"].extend(card["r3"]["blockers"][:3])
            # R3 hard failures override attractive R2 economics for next action
            if card["r3"].get("recommendation") == "DO_NOT_BID" or card["r3"].get("blockers"):
                card["next_action"] = card["r3"].get("next_action") or card.get("next_action")
                if card["r3"].get("recommendation") == "DO_NOT_BID":
                    card["operator_status"] = "Compliance Blocked"
            elif card["r3"].get("next_action"):
                card["next_action"] = card["r3"]["next_action"]
    except Exception:
        card["r3"] = None
    # R4 response package panel
    try:
        from response_engine.r4_service import r4_operator_card

        if project.get("generated_package") or project.get("r4_analyzed_at"):
            card["r4"] = r4_operator_card(project)
            card["checklist"].append(
                {
                    "id": "r4_package",
                    "label": f"Response package: {card['r4'].get('package_status') or 'NOT_STARTED'}",
                    "done": card["r4"].get("package_status")
                    in {"DRAFT_COMPLETE", "OWNER_SIGNATURE_REQUIRED", "READY_FOR_R5_PREFLIGHT"},
                }
            )
            if card["r4"].get("blockers"):
                card["missing_items"].extend(card["r4"]["blockers"][:3])
            if card["r4"].get("next_action") and not (
                card.get("r3") and card["r3"].get("recommendation") == "DO_NOT_BID"
            ):
                card["next_action"] = card["r4"]["next_action"]
        else:
            card["r4"] = {
                "package_status": "NOT_STARTED",
                "next_action": "BUILD RESPONSE PACKAGE",
                "label": "DRAFT — NOT SUBMITTED",
                "never_ready_to_submit": True,
            }
    except Exception:
        card["r4"] = None
    # R5 submission workflow + canonical operator state (plain language)
    try:
        from response_engine.operator_state_service import build_operator_state
        from response_engine.r5_service import r5_operator_card

        if project.get("r5_preflight") or project.get("generated_package"):
            card["r5"] = r5_operator_card(project)
        state = build_operator_state(project, base_card=card)
        card["operator_state"] = state
        card["plain_status"] = state.get("plain_status")
        card["stages"] = state.get("stages")
        if state.get("next_action"):
            card["next_action"] = state["next_action"].get("action_label")
            card["ui_next_action"] = state["next_action"].get("action_type")
            card["ui_next_action_label"] = state["next_action"].get("action_label")
            card["ui_next_action_reason"] = state["next_action"].get("reason")
            card["assigned_role"] = state["next_action"].get("assigned_role")
        if state.get("plain_status"):
            card["operator_status"] = state["plain_status"]
    except Exception:
        card["r5"] = None
    return card


def _public_project(project: dict[str, Any]) -> dict[str, Any]:
    """Strip full document text from default API unless advanced requested."""
    docs = []
    for d in project.get("documents") or []:
        docs.append({k: v for k, v in d.items() if k != "text"})
    out = {**project, "documents": docs}
    return out


def _extract_deadlines(project: dict[str, Any], text: str) -> None:
    m = re.search(
        r"(?:bid|offer|proposal|quote|response)\s+(?:due|deadline|closing)[:\s]+([^\n]{5,100})",
        text,
        re.I,
    )
    if m and not project.get("submission_deadline"):
        project["submission_deadline"] = m.group(1).strip()
        tz = None
        chunk = m.group(0).upper()
        for token, name in (
            ("ET", "America/New_York"),
            ("EST", "America/New_York"),
            ("EDT", "America/New_York"),
            ("CT", "America/Chicago"),
            ("PT", "America/Los_Angeles"),
            ("UTC", "UTC"),
        ):
            if re.search(rf"\b{token}\b", chunk):
                tz = name
                break
        if tz:
            project["submission_timezone"] = tz
    qm = re.search(r"questions?\s+(?:due|deadline)[:\s]+([^\n]{5,100})", text, re.I)
    if qm and not project.get("question_deadline"):
        project["question_deadline"] = qm.group(1).strip()


def _ensure_amendment_ack_requirement(project: dict[str, Any]) -> None:
    for req in project.get("requirements") or []:
        if req.get("requirement_category") == "AMENDMENT_ACK" and not req.get("superseded"):
            return
    doc_id = None
    for d in project.get("documents") or []:
        if d.get("document_type") == "AMENDMENT":
            doc_id = d["document_id"]
            break
    numbers = ", ".join(str(a.get("number")) for a in project.get("amendments") or [])
    req = new_requirement(
        response_project_id=project["response_project_id"],
        requirement_text=f"Acknowledge all amendments ({numbers})",
        requirement_category="AMENDMENT_ACK",
        source_document_id=doc_id,
        mandatory=True,
        materiality="MATERIAL",
        provenance=empty_provenance(
            document_id=doc_id,
            excerpt=f"Acknowledge amendments {numbers}",
            extractor="r1_amendment_ack",
            confidence="HIGH",
        ),
        confidence="HIGH",
    )
    merge_requirements(project, [req])


# Re-exports for API convenience
__all__ = [
    "create_or_get_project_from_opportunity",
    "compile_project",
    "apply_amendment_quantity_change",
    "get_project_view",
    "bid_prep_card_for_opportunity",
    "ingest_document",
    "ingest_document_set",
    "set_requirement_answer",
    "evaluate_exact_brand_mismatch",
    "evaluate_salient_generic_equal_claim",
    "ingest_supplier_quote_as_internal",
    "firewall_report",
    "load_project",
    "save_project",
    "find_by_opportunity",
    "BUILD",
]
