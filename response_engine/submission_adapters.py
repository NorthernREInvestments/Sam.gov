"""R5 submission adapters — DRY_RUN by default. No live portal/email/physical send in tests."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r5_constants import (
    ADAPTER_DIBBS,
    ADAPTER_EMAIL,
    ADAPTER_PHYSICAL,
    ADAPTER_PIEE,
    ADAPTER_PORTAL,
    BUILD,
    DRY_RUN_SUBMITTED_CONFIRMED,
    SUB_IN_PROGRESS,
    SUB_NOT_STARTED,
)


def _utc() -> str:
    return now_utc().isoformat()


def detect_adapter_type(project: dict[str, Any]) -> str:
    sub = (project.get("submission_system") or project.get("portal") or "").upper()
    if "DIBBS" in sub:
        return ADAPTER_DIBBS
    if "PIEE" in sub:
        return ADAPTER_PIEE
    if "EMAIL" in sub or (project.get("response_type") or "").upper() == "EMAIL":
        return ADAPTER_EMAIL
    if "PHYSICAL" in sub or "MAIL" in sub:
        return ADAPTER_PHYSICAL
    if sub:
        return ADAPTER_PORTAL
    return ADAPTER_PORTAL


def build_submission_plan(project: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Guided submission checklist — plain language, no auto-submit."""
    adapter = detect_adapter_type(project)
    freeze = project.get("frozen_submission_package") or {}
    handoff = project.get("submission_handoff") or {}
    pkg = project.get("generated_package") or {}
    files = freeze.get("files") or [
        {"filename": d.get("filename"), "hash": d.get("hash")}
        for d in (pkg.get("generated_documents") or [])
        if d.get("buyer_facing", True)
    ]
    portal_ds = freeze.get("portal_response_dataset") or handoff.get("portal_response_dataset") or pkg.get("portal_response_dataset")
    email_ds = freeze.get("email_dataset") or handoff.get("email_dataset")
    physical = freeze.get("physical_instructions") or handoff.get("physical_submission_instructions")

    steps = _steps_for(adapter, project, files, portal_ds, email_ds, physical)
    plan = {
        "kind": "SubmissionPlan",
        "plan_id": new_id("SPL"),
        "adapter": adapter,
        "dry_run": dry_run,
        "destination": project.get("submission_system") or project.get("portal") or adapter,
        "deadline": project.get("submission_deadline"),
        "timezone": project.get("submission_timezone"),
        "files": files,
        "steps": steps,
        "final_submit_requires_owner": True,
        "status": SUB_NOT_STARTED,
        "label": "TEST SUBMISSION — NOT SENT" if dry_run else "GUIDED SUBMISSION",
        "build": BUILD,
        "created_at": _utc(),
        "note": "R5 prepares guided steps only. Default dry_run=True. No MFA/CAPTCHA bypass.",
    }
    project["submission_plan"] = plan
    return plan


def _steps_for(adapter: str, project: dict, files: list, portal_ds, email_ds, physical) -> list[dict[str, Any]]:
    steps = []
    if adapter in (ADAPTER_PORTAL, ADAPTER_DIBBS, ADAPTER_PIEE):
        steps.append({"n": 1, "action": "OPEN PORTAL", "detail": f"Open {project.get('submission_system') or adapter}", "complete": False})
        steps.append({"n": 2, "action": "FIND SOLICITATION", "detail": project.get("solicitation_number") or project.get("title"), "complete": False})
        for i, f in enumerate(files[:10], start=3):
            steps.append({"n": i, "action": "UPLOAD FILE", "detail": f.get("filename"), "hash": f.get("hash"), "complete": False})
        n = len(steps) + 1
        price = (project.get("owner_submission_approval") or {}).get("total_offer") or (
            project.get("frozen_submission_package") or {}
        ).get("selected_price")
        if price:
            steps.append({"n": n, "action": "ENTER TOTAL PRICE", "detail": str(price), "source": "Approved pricing scenario", "complete": False})
            n += 1
        steps.append({"n": n, "action": "STOP FOR OWNER FINAL SUBMIT", "detail": "Do not click final submit without owner", "complete": False})
        steps.append({"n": n + 1, "action": "CAPTURE CONFIRMATION", "detail": "Save confirmation number / receipt", "complete": False})
    elif adapter == ADAPTER_EMAIL:
        steps.append({"n": 1, "action": "REVIEW EMAIL DRAFT", "detail": (email_ds or {}).get("subject"), "complete": False})
        steps.append({"n": 2, "action": "ATTACH FILES", "detail": ", ".join(f.get("filename") or "" for f in files[:8]), "complete": False})
        steps.append({"n": 3, "action": "STOP — DO NOT SEND WITHOUT OWNER", "detail": "Email not auto-sent", "complete": False})
        steps.append({"n": 4, "action": "CAPTURE SENT RECEIPT", "detail": "Save sent confirmation", "complete": False})
    else:
        steps.append({"n": 1, "action": "PRINT / PACKAGE", "detail": (physical or {}).get("note") or "Follow physical checklist", "complete": False})
        steps.append({"n": 2, "action": "VERIFY ADDRESS", "detail": (physical or {}).get("address") or "UNKNOWN — confirm from solicitation", "complete": False})
        steps.append({"n": 3, "action": "SHIP / DELIVER", "detail": "Human action only", "complete": False})
        steps.append({"n": 4, "action": "CAPTURE DELIVERY RECEIPT", "detail": "Tracking / delivery confirmation", "complete": False})
    return steps


def mark_step_complete(project: dict[str, Any], step_n: int) -> dict[str, Any]:
    plan = project.get("submission_plan")
    if not plan:
        return {"ok": False, "error": "no_plan"}
    for s in plan.get("steps") or []:
        if s.get("n") == step_n:
            s["complete"] = True
            s["completed_at"] = _utc()
    plan["status"] = SUB_IN_PROGRESS
    return {"ok": True, "plan": plan}


def dry_run_submit(project: dict[str, Any], *, submitted_by: str = "test") -> dict[str, Any]:
    """Synthetic submission for fixtures only — never contacts external systems."""
    plan = project.get("submission_plan") or build_submission_plan(project, dry_run=True)
    for s in plan.get("steps") or []:
        s["complete"] = True
    plan["status"] = DRY_RUN_SUBMITTED_CONFIRMED
    plan["dry_run_completed_at"] = _utc()
    plan["submitted_by"] = submitted_by
    project["submission_plan"] = plan
    return {
        "ok": True,
        "status": DRY_RUN_SUBMITTED_CONFIRMED,
        "label": "TEST SUBMISSION — NOT SENT",
        "external_side_effects": 0,
        "plan": plan,
    }


def validate_portal_price_entry(project: dict[str, Any], entered_price: Any) -> dict[str, Any]:
    approved = (project.get("owner_submission_approval") or {}).get("total_offer") or (
        project.get("frozen_submission_package") or {}
    ).get("selected_price")
    if approved is None:
        return {"ok": False, "error": "no_approved_price"}
    try:
        if abs(float(str(approved).replace(",", "")) - float(str(entered_price).replace(",", ""))) > 0.01:
            return {
                "ok": False,
                "error": "SUBMISSION_DATA_CONFLICT",
                "approved": approved,
                "entered": entered_price,
                "plain": "Portal price does not match approved offer — hard stop",
            }
    except (TypeError, ValueError):
        return {"ok": False, "error": "unparseable_price"}
    return {"ok": True, "approved": approved, "entered": entered_price}
