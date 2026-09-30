"""R4 orchestration — response plan → field maps → generate drafts → package → R5 handoff.

Canonical response-output generation for Bid Prep.
Never submits, never auto-signs, never invents facts, 0 SAM API.
Max state: READY_FOR_R5_PREFLIGHT.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

from response_engine.company_profile_r3 import load_company_compliance_profile
from response_engine.document_generators import (
    build_email_response_dataset,
    build_physical_submission_dataset,
    build_portal_response_dataset,
    generate_certification_summary,
    generate_docx_bidder_info,
    generate_product_schedule,
    generate_quote_letter,
    generate_technical_matrix,
    generate_technical_narrative_draft,
    populate_pdf_safe_fields,
    scan_text_for_leaks,
)
from response_engine.field_map import build_field_maps, summarize_field_maps
from response_engine.package_store import (
    assemble_buyer_zip,
    build_manifest,
    new_generated_package,
    new_submission_handoff,
    next_version,
    package_dir,
    write_internal_manifest,
)
from response_engine.r4_constants import (
    BLOCKED,
    BUILD,
    BUYER_PDF_FORM,
    BUYER_XLSX_TEMPLATE,
    DOCUMENT_REVIEW_REQUIRED,
    DRAFT_COMPLETE,
    DRAFT_INCOMPLETE,
    DRAFT_LABEL,
    EMAIL_RESPONSE_DATA,
    FORM_MAPPING_REQUIRED,
    GENERATED_CERTIFICATION,
    GENERATED_COMPLIANCE_MATRIX,
    GENERATED_PRICING_SCHEDULE,
    GENERATED_PRODUCT_SCHEDULE,
    GENERATED_QUOTE_LETTER,
    GENERATED_TECHNICAL_RESPONSE,
    GENERATION_BLOCKED,
    OWNER_ATTESTATION_REQUIRED,
    OWNER_INPUT_REQUIRED,
    OWNER_PRICE_APPROVAL_REQUIRED,
    OWNER_SIGNATURE_REQUIRED,
    OWNER_SIGNATURE_REQUIRED_FIELD,
    PHYSICAL_PACKAGE_INSTRUCTION,
    PORTAL_RESPONSE_DATA,
    READY_FOR_R5_PREFLIGHT,
    RESPONSE_CONFLICT,
    RESPONSE_INPUT_INCOMPLETE,
    STALE_DUE_TO_AMENDMENT,
    STALE_DUE_TO_COMPANY_PROFILE_CHANGE,
    STALE_DUE_TO_PRICE_CHANGE,
    STALE_DUE_TO_PRODUCT_CHANGE,
)
from response_engine.response_plan import build_response_plan
from response_engine.spreadsheet_fill import create_pricing_schedule_xlsx, populate_xlsx_from_maps, validate_extended_totals
from response_engine.store import load_project, save_project


def _utc() -> str:
    return now_utc().isoformat()


def run_r4_generation(
    project: dict[str, Any],
    *,
    persist: bool = True,
    force: bool = False,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Canonical R4 generation. Fail-closed. Never READY_TO_SUBMIT."""
    project["r4_build"] = BUILD
    project["r4_analyzed_at"] = _utc()
    project["r4_sam_api_calls"] = 0

    profile = load_company_compliance_profile()
    plan = build_response_plan(project)
    project["response_plan"] = plan

    selected = _resolve_selected_scenario(project)
    field_maps = build_field_maps(project, profile=profile, selected_scenario=selected)
    fmap_summary = summarize_field_maps(field_maps)
    project["response_field_maps"] = field_maps

    blockers = list(plan.get("blockers") or [])
    unresolved = [m for m in field_maps if m.get("generation_status") in (BLOCKED, "UNKNOWN", OWNER_ATTESTATION_REQUIRED)]
    att_pending = [m for m in field_maps if m.get("generation_status") == OWNER_ATTESTATION_REQUIRED]
    sigs = [m for m in field_maps if m.get("generation_status") == OWNER_SIGNATURE_REQUIRED_FIELD]

    # Hard generation blocks
    if plan.get("status") == "BLOCKED":
        blockers.extend(plan.get("blockers") or [])
    if any(m.get("generation_status") == BLOCKED and "quantity" in str(m.get("source_requirement")) for m in field_maps):
        blockers.append("Line quantity unknown — pricing generation blocked")
    if att_pending and _certs_required(plan):
        blockers.append(f"{len(att_pending)} owner attestation(s) required before certification population")

    readiness = _compute_readiness(
        project=project,
        plan=plan,
        fmap_summary=fmap_summary,
        blockers=blockers,
        att_pending=att_pending,
        selected=selected,
    )

    version = next_version(project["response_project_id"])
    pkg_dir = package_dir(project["response_project_id"], version)
    generated_docs: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    firewall_leaks: list[str] = []

    # Always generate what we can as DRAFT_INCOMPLETE when partial
    buyer_facing_paths: list[str] = []

    for item in plan.get("deliverables") or []:
        dtype = item.get("deliverable_type")
        try:
            result = _generate_deliverable(
                dtype=dtype,
                project=project,
                profile=profile,
                field_maps=field_maps,
                pkg_dir=pkg_dir,
                plan_item=item,
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:300], "document_type": dtype}
        if result:
            doc = {
                "document_type": dtype,
                "filename": result.get("filename") or Path(result.get("output_path") or "x").name,
                "path": result.get("output_path"),
                "hash": result.get("output_hash"),
                "status": result.get("status") or ("OK" if result.get("ok") else "FAILED"),
                "buyer_facing": result.get("buyer_facing", True),
                "owner_signature_required": result.get("signature_status") == OWNER_SIGNATURE_REQUIRED_FIELD
                or item.get("signature_required"),
                "owner_attestation_pending": bool(att_pending) and dtype == GENERATED_CERTIFICATION,
                "source_origin": "R4",
                "generated_from": ["R1", "R2", "R3"],
                "label": DRAFT_LABEL,
                "detail": {k: v for k, v in result.items() if k not in {"written"}},
            }
            generated_docs.append(doc)
            if doc.get("buyer_facing") and doc.get("path"):
                buyer_facing_paths.append(doc["path"])
            for leak in result.get("leaks") or []:
                firewall_leaks.append(leak)

    # Portal / email / physical datasets (JSON, buyer-facing structure only)
    portal_ds = build_portal_response_dataset(project, field_maps)
    email_ds = build_email_response_dataset(project, [Path(p).name for p in buyer_facing_paths])
    physical_ds = build_physical_submission_dataset(project)
    for name, ds in (
        ("portal_response_dataset.json", portal_ds),
        ("email_response_dataset.json", email_ds),
        ("physical_submission_instructions.json", physical_ds),
    ):
        p = pkg_dir / "buyer_facing" / name
        p.write_text(json.dumps(ds, indent=2, default=str), encoding="utf-8")
        generated_docs.append(
            {
                "document_type": PORTAL_RESPONSE_DATA if "portal" in name else EMAIL_RESPONSE_DATA if "email" in name else PHYSICAL_PACKAGE_INSTRUCTION,
                "filename": name,
                "path": str(p),
                "hash": __import__("hashlib").sha256(p.read_bytes()).hexdigest(),
                "status": "OK",
                "buyer_facing": True,
                "label": DRAFT_LABEL,
            }
        )

    # Conflicts across outputs (data conflicts hard; filename collisions are warnings)
    all_conflicts = _detect_conflicts(project, field_maps, generated_docs)
    hard_conflicts = [c for c in all_conflicts if c.get("type") != "FILENAME_COLLISION"]
    soft_conflicts = [c for c in all_conflicts if c.get("type") == "FILENAME_COLLISION"]
    conflicts = hard_conflicts
    if hard_conflicts:
        readiness = RESPONSE_CONFLICT
    warnings = list(soft_conflicts)

    # Totals validation
    unit_prices = {
        m.get("source_requirement", "").replace("line.", "").replace(".unit_price", ""): m.get("source_value")
        for m in field_maps
        if "unit_price" in str(m.get("source_requirement"))
    }
    total_issues = validate_extended_totals(project.get("line_items") or [], unit_prices)

    manifest = build_manifest(generated_docs)
    write_internal_manifest(pkg_dir, manifest)
    zip_info = assemble_buyer_zip(
        pkg_dir,
        [d for d in generated_docs if d.get("buyer_facing")],
    )

    # Package status
    if blockers and not generated_docs:
        pkg_status = GENERATION_BLOCKED
    elif blockers or unresolved or att_pending:
        pkg_status = DRAFT_INCOMPLETE
    elif sigs and readiness not in (RESPONSE_CONFLICT, GENERATION_BLOCKED):
        pkg_status = OWNER_SIGNATURE_REQUIRED
    elif readiness == READY_FOR_R5_PREFLIGHT:
        pkg_status = READY_FOR_R5_PREFLIGHT
    elif readiness == DRAFT_COMPLETE:
        pkg_status = DRAFT_COMPLETE
    else:
        pkg_status = DRAFT_INCOMPLETE

    # Elevate to READY_FOR_R5_PREFLIGHT when checklist clear enough
    if (
        pkg_status in (DRAFT_COMPLETE, OWNER_SIGNATURE_REQUIRED)
        and not blockers
        and not conflicts
        and not firewall_leaks
        and fmap_summary.get("blocked", 0) == 0
        and not att_pending
        and _r123_current(project)
    ):
        pkg_status = READY_FOR_R5_PREFLIGHT
        readiness = READY_FOR_R5_PREFLIGHT

    package = new_generated_package(
        response_project_id=project.get("response_project_id"),
        generation_version=version,
        r1_version=project.get("compiled_at") or project.get("r1_build"),
        r2_version=project.get("r2_analyzed_at") or project.get("r2_build"),
        r3_version=project.get("r3_analyzed_at") or project.get("company_profile_version"),
        controlling_document_version=project.get("controlling_document_version"),
        created_by=created_by,
        package_status=pkg_status,
        response_type=project.get("response_type"),
        submission_system=project.get("submission_system") or project.get("portal"),
        generated_documents=generated_docs,
        portal_response_dataset=portal_ds,
        unresolved_fields=[m.get("field_map_id") for m in unresolved],
        owner_signature_requirements=[m.get("field_map_id") for m in sigs],
        owner_attestations_pending=[m.get("field_map_id") for m in att_pending],
        material_generation_blockers=blockers,
        package_hash_manifest=manifest.get("entries"),
        notes=DRAFT_LABEL,
    )
    package["zip"] = zip_info
    package["field_map_summary"] = fmap_summary
    package["conflicts"] = conflicts
    package["warnings"] = warnings
    package["total_validation_issues"] = total_issues
    package["firewall_leaks"] = firewall_leaks
    package["readiness"] = readiness
    package["response_plan_id"] = plan.get("plan_id")
    package["package_dir"] = str(pkg_dir)
    package["selected_scenario_id"] = (selected or {}).get("scenario_id")

    handoff = new_submission_handoff(
        response_project_id=project.get("response_project_id"),
        package_version=version,
        package_id=package["package_id"],
        clean_buyer_facing_files=[d for d in generated_docs if d.get("buyer_facing")],
        portal_response_dataset=portal_ds,
        email_dataset=email_ds,
        physical_instructions=physical_ds,
        signatures_outstanding=[m for m in sigs],
        owner_approvals_outstanding=list(att_pending),
        legal_deadline=project.get("submission_deadline"),
        submission_system=project.get("submission_system") or project.get("portal"),
        package_hashes=[{"filename": e.get("filename"), "hash": e.get("hash")} for e in manifest.get("entries") or []],
    )

    # Supersede prior package
    if project.get("generated_package"):
        project.setdefault("generated_package_history", []).append(
            {**project["generated_package"], "package_status": "SUPERSEDED"}
        )
    project["generated_package"] = package
    project["submission_handoff"] = handoff
    project["r4_readiness"] = readiness
    project["r4_package_status"] = pkg_status
    project["r4_blockers"] = blockers
    project["r4_next_action"] = _next_action(readiness, blockers, att_pending, sigs, firewall_leaks)
    project["r4_firewall"] = {
        "leaks": firewall_leaks,
        "ok": len(firewall_leaks) == 0,
        "supplier_cost": 0,
        "max_buy": 0,
        "target_profit": 0,
        "margin": 0,
        "historical_price": 0,
        "financing": 0,
        "supplier_boilerplate": 0,
        "internal_notes": 0,
        **{k: firewall_leaks.count(k) for k in (
            "supplier_cost", "max_buy", "target_profit", "target_margin",
            "historical_government_price", "financing_strategy",
            "supplier_boilerplate", "internal_notes",
        )},
    }

    if persist:
        save_project(project)
    return package


def select_bid_price_scenario(project: dict[str, Any], scenario_id: str, *, approve_for_draft: bool = True) -> dict[str, Any]:
    """Owner/operator selects scenario for R4 drafting — not final submission approval."""
    found = None
    for s in project.get("pricing_scenarios") or []:
        s["selected_for_draft"] = False
        s["approved_for_r4_draft"] = False
        if s.get("scenario_id") == scenario_id:
            s["selected_for_draft"] = True
            s["approved_for_r4_draft"] = approve_for_draft
            found = s
    if not found:
        return {"ok": False, "error": "scenario_not_found"}
    project["selected_bid_price_scenario_id"] = scenario_id
    project["selected_bid_price_scenario"] = {
        "kind": "SelectedBidPriceScenario",
        "scenario_id": scenario_id,
        "approved_for_r4_draft": approve_for_draft,
        "final_submission_approval": False,
        "selected_at": _utc(),
    }
    invalidate_r4(project, reason=STALE_DUE_TO_PRICE_CHANGE)
    save_project(project)
    return {"ok": True, "scenario": found}


def invalidate_r4(project: dict[str, Any], *, reason: str) -> None:
    project["r4_stale_reason"] = reason
    project["r4_package_status"] = reason if reason.startswith("STALE_") else STALE_DUE_TO_AMENDMENT
    if project.get("generated_package"):
        project["generated_package"]["package_status"] = reason if reason.startswith("STALE_") else STALE_DUE_TO_AMENDMENT
        project["generated_package"]["stale"] = True
        project["generated_package"]["stale_reason"] = reason


def r4_operator_card(project: dict[str, Any]) -> dict[str, Any]:
    pkg = project.get("generated_package") or {}
    plan = project.get("response_plan") or {}
    docs = pkg.get("generated_documents") or []
    fw = project.get("r4_firewall") or {}
    return {
        "build": BUILD,
        "title": "RESPONSE PACKAGE",
        "label": DRAFT_LABEL,
        "package_status": pkg.get("package_status") or project.get("r4_package_status") or "NOT_STARTED",
        "readiness": project.get("r4_readiness"),
        "documents_required": len(plan.get("deliverables") or []),
        "documents_generated": len(docs),
        "owner_signature_required": len(pkg.get("owner_signature_requirements") or []),
        "owner_attestations_pending": len(pkg.get("owner_attestations_pending") or []),
        "pricing": {
            "scenario_id": pkg.get("selected_scenario_id"),
            "totals_issues": len(pkg.get("total_validation_issues") or []),
        },
        "technical": {
            "matrices": sum(1 for d in docs if d.get("document_type") == GENERATED_COMPLIANCE_MATRIX),
            "narratives": sum(1 for d in docs if d.get("document_type") == GENERATED_TECHNICAL_RESPONSE),
        },
        "company": {
            "blockers": project.get("r4_blockers") or [],
        },
        "blockers": project.get("r4_blockers") or [],
        "next_action": project.get("r4_next_action") or "BUILD RESPONSE PACKAGE",
        "firewall_ok": fw.get("ok", True),
        "zip": (pkg.get("zip") or {}).get("zip_path"),
        "never_ready_to_submit": True,
        "handoff_ready": bool(project.get("submission_handoff")),
    }


def get_r4_view(response_project_id: str) -> dict[str, Any]:
    project = load_project(response_project_id)
    if not project:
        return {"ok": False, "error": "not_found"}
    return {
        "ok": True,
        "plan": project.get("response_plan"),
        "package": project.get("generated_package"),
        "field_maps": project.get("response_field_maps"),
        "handoff": project.get("submission_handoff"),
        "operator": r4_operator_card(project),
        "readiness": project.get("r4_readiness"),
        "sam_api_calls": 0,
    }


# --- helpers ---


def _resolve_selected_scenario(project: dict[str, Any]) -> dict[str, Any] | None:
    from response_engine.field_map import _selected_scenario

    return _selected_scenario(project)


def _certs_required(plan: dict[str, Any]) -> bool:
    return any(d.get("deliverable_type") == GENERATED_CERTIFICATION for d in plan.get("deliverables") or [])


def _r123_current(project: dict[str, Any]) -> bool:
    return bool(project.get("documents") or project.get("requirements")) and not project.get("package_stale")


def _compute_readiness(**ctx: Any) -> str:
    blockers = ctx["blockers"]
    fmap = ctx["fmap_summary"]
    att = ctx["att_pending"]
    selected = ctx["selected"]
    plan = ctx["plan"]
    project = ctx["project"]

    if blockers and not (project.get("line_items") or project.get("requirements")):
        return RESPONSE_INPUT_INCOMPLETE
    if fmap.get("by_status", {}).get("FORM_MAPPING_REQUIRED"):
        return FORM_MAPPING_REQUIRED
    if att:
        return OWNER_ATTESTATION_REQUIRED
    if not selected and any(
        d.get("deliverable_type") in {BUYER_XLSX_TEMPLATE, GENERATED_PRICING_SCHEDULE, GENERATED_QUOTE_LETTER}
        for d in plan.get("deliverables") or []
    ):
        # Can still draft incomplete package
        if project.get("require_owner_price_approval"):
            return OWNER_PRICE_APPROVAL_REQUIRED
    if blockers:
        return GENERATION_BLOCKED if fmap.get("blocked") else DRAFT_INCOMPLETE
    if fmap.get("owner_signature"):
        return OWNER_SIGNATURE_REQUIRED
    return DRAFT_COMPLETE


def _next_action(readiness: str, blockers: list, att: list, sigs: list, leaks: list) -> str:
    if leaks:
        return "REMOVE INTERNAL LEAKS FROM DRAFT"
    if readiness == OWNER_ATTESTATION_REQUIRED or att:
        return "CONFIRM OWNER ATTESTATIONS"
    if readiness == OWNER_PRICE_APPROVAL_REQUIRED:
        return "SELECT / APPROVE PRICE SCENARIO FOR DRAFT"
    if blockers:
        return blockers[0][:80] if blockers else "RESOLVE BLOCKERS"
    if readiness == FORM_MAPPING_REQUIRED:
        return "MAP BUYER PRICING CELLS"
    if readiness in (DRAFT_COMPLETE, OWNER_SIGNATURE_REQUIRED, READY_FOR_R5_PREFLIGHT):
        return "REVIEW PACKAGE → R5 PREFLIGHT"
    return "BUILD RESPONSE PACKAGE"


def _generate_deliverable(
    *,
    dtype: str,
    project: dict[str, Any],
    profile: dict[str, Any],
    field_maps: list[dict[str, Any]],
    pkg_dir: Path,
    plan_item: dict[str, Any],
) -> dict[str, Any] | None:
    out_dir = pkg_dir / "buyer_facing"
    if dtype == GENERATED_QUOTE_LETTER:
        p = out_dir / "Quote_Letter_DRAFT.txt"
        return {**generate_quote_letter(project, out_path=p, field_maps=field_maps, profile=profile), "filename": p.name}
    if dtype == GENERATED_PRODUCT_SCHEDULE:
        p = out_dir / "Product_Schedule_DRAFT.csv"
        return {**generate_product_schedule(project, out_path=p), "filename": p.name}
    if dtype == GENERATED_COMPLIANCE_MATRIX:
        p = out_dir / "Technical_Compliance_Matrix_DRAFT.csv"
        return {**generate_technical_matrix(project, out_path=p), "filename": p.name}
    if dtype == GENERATED_TECHNICAL_RESPONSE:
        p = out_dir / "Technical_Response_DRAFT.txt"
        return {**generate_technical_narrative_draft(project, out_path=p), "filename": p.name}
    if dtype == GENERATED_CERTIFICATION:
        p = out_dir / "Certification_Summary_DRAFT.json"
        return {**generate_certification_summary(project, out_path=p), "filename": p.name}
    if dtype == GENERATED_PRICING_SCHEDULE:
        p = out_dir / "Pricing_Schedule_DRAFT.xlsx"
        return {
            **create_pricing_schedule_xlsx(
                lines=project.get("line_items") or [],
                field_maps=field_maps,
                out_path=p,
                company_name=profile.get("legal_name"),
            ),
            "filename": p.name,
        }
    if dtype == BUYER_XLSX_TEMPLATE:
        return _fill_buyer_xlsx(project, field_maps, pkg_dir, plan_item)
    if dtype == BUYER_PDF_FORM:
        return _fill_buyer_pdf(project, field_maps, pkg_dir, plan_item)
    if dtype in {PORTAL_RESPONSE_DATA, EMAIL_RESPONSE_DATA, PHYSICAL_PACKAGE_INSTRUCTION}:
        return None  # handled centrally
    # Bidder info DOCX as useful default for forms
    if dtype in ("BUYER_DOCX_FORM", "FORM"):
        p = out_dir / "Bidder_Information_DRAFT.docx"
        return {**generate_docx_bidder_info(project, out_path=p, field_maps=field_maps), "filename": p.name}
    return {
        "ok": False,
        "status": "MANUAL_GENERATION_REQUIRED",
        "document_type": dtype,
        "filename": plan_item.get("filename") or dtype,
        "buyer_facing": False,
        "note": f"Deliverable type {dtype} requires manual/R5 handling",
    }


def _fill_buyer_xlsx(project, field_maps, pkg_dir, plan_item) -> dict[str, Any]:
    doc_id = plan_item.get("source")
    original_path = None
    original_bytes = None
    for doc in project.get("documents") or []:
        if doc.get("document_id") == doc_id or (plan_item.get("filename") and doc.get("filename") == plan_item.get("filename")):
            bp = doc.get("binary_path")
            if bp and Path(bp).exists():
                original_path = Path(bp)
            break
    out = pkg_dir / "buyer_facing" / (plan_item.get("filename") or "Buyer_Pricing_DRAFT.xlsx")
    if original_path is None:
        # Fall back to generated schedule rather than inventing buyer layout
        return {
            **create_pricing_schedule_xlsx(
                lines=project.get("line_items") or [],
                field_maps=field_maps,
                out_path=out.with_suffix(".xlsx") if out.suffix.lower() not in {".xlsx", ".xlsm"} else out,
            ),
            "filename": out.name,
            "note": "Buyer binary missing — generated M3 pricing schedule instead",
        }
    # Preserve original
    orig_dir = pkg_dir / "originals"
    from response_engine.spreadsheet_fill import copy_original_immutable

    copy_original_immutable(original_path, orig_dir)
    is_xlsm = original_path.suffix.lower() == ".xlsm"
    return {
        **populate_xlsx_from_maps(
            original_path=original_path,
            original_bytes=None,
            field_maps=field_maps,
            out_path=out,
            is_xlsm=is_xlsm,
        ),
        "filename": out.name,
    }


def _fill_buyer_pdf(project, field_maps, pkg_dir, plan_item) -> dict[str, Any]:
    original_path = None
    for doc in project.get("documents") or []:
        if doc.get("document_id") == plan_item.get("source") or (
            plan_item.get("filename") and doc.get("filename") == plan_item.get("filename")
        ):
            bp = doc.get("binary_path")
            if bp and Path(bp).exists():
                original_path = Path(bp)
            break
    out = pkg_dir / "buyer_facing" / (plan_item.get("filename") or "Buyer_Form_DRAFT.pdf")
    if original_path is None:
        # Generate bidder info DOCX/text instead of inventing PDF
        p = pkg_dir / "buyer_facing" / "Bidder_Information_DRAFT.docx"
        return {
            **generate_docx_bidder_info(project, out_path=p, field_maps=field_maps),
            "filename": p.name,
            "note": "Buyer PDF binary missing — bidder info DOCX generated",
        }
    from response_engine.spreadsheet_fill import copy_original_immutable

    copy_original_immutable(original_path, pkg_dir / "originals")
    return {**populate_pdf_safe_fields(original_path=original_path, original_bytes=None, field_maps=field_maps, out_path=out), "filename": out.name}


def _detect_conflicts(project, field_maps, docs) -> list[dict[str, Any]]:
    conflicts = []
    # Delivery conflicts: manual override vs R2
    for ov in project.get("manual_response_overrides") or []:
        if ov.get("field") == "delivery_days":
            r2_days = project.get("verified_delivery_days")
            if r2_days and str(ov.get("value")) != str(r2_days):
                conflicts.append(
                    {
                        "type": "RESPONSE_DATA_CONFLICT",
                        "field": "delivery_days",
                        "manual": ov.get("value"),
                        "verified": r2_days,
                    }
                )
    # Filename collisions
    names = [d.get("filename") for d in docs if d.get("filename")]
    for n in set(names):
        if names.count(n) > 1:
            conflicts.append({"type": "FILENAME_COLLISION", "filename": n})
    return conflicts


__all__ = [
    "run_r4_generation",
    "select_bid_price_scenario",
    "invalidate_r4",
    "r4_operator_card",
    "get_r4_view",
]
