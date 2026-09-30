"""R5 orchestration — preflight → approval → freeze → guided submission → receipt.

Canonical submission workflow. DRY_RUN default. 0 SAM. 0 external side effects in tests.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

from response_engine.operator_state_service import build_operator_state
from response_engine.r5_approval import (
    create_approval_request,
    decide_owner_approval,
    freeze_submission_package,
    invalidate_approval,
)
from response_engine.r5_constants import BUILD, DRY_RUN_SUBMITTED_CONFIRMED
from response_engine.r5_preflight import run_preflight
from response_engine.r5_signatures import auto_signed_count, ensure_signature_tasks, mark_signature_signed
from response_engine.store import load_project, save_project
from response_engine.submission_adapters import (
    build_submission_plan,
    dry_run_submit,
    mark_step_complete,
    validate_portal_price_entry,
)
from response_engine.submission_audit import new_submission_event, record_receipt, write_submission_audit


def _utc() -> str:
    return now_utc().isoformat()


def run_r5_preflight(project: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    ensure_signature_tasks(project)
    result = run_preflight(project)
    project["r5_build"] = BUILD
    project["r5_preflight_at"] = _utc()
    if result.get("approval_eligible") and not project.get("owner_submission_approval"):
        create_approval_request(project, preflight=result)
    elif result.get("approval_eligible"):
        # Refresh pending request for current package
        ap = project.get("owner_submission_approval") or {}
        if ap.get("approval_status") in (None, "PENDING", "EXPIRED_DUE_TO_CHANGE"):
            create_approval_request(project, preflight=result)
    if persist:
        save_project(project)
    return result


def r5_owner_approve(project: dict[str, Any], *, decision: str, approved_by: str, reason: str | None = None) -> dict[str, Any]:
    if not project.get("r5_preflight"):
        run_r5_preflight(project, persist=False)
    result = decide_owner_approval(project, decision=decision, approved_by=approved_by, reason=reason)
    if result.get("ok") and decision.upper() in ("APPROVED", "APPROVED"):
        freeze_submission_package(project)
        build_submission_plan(project, dry_run=True)
    save_project(project)
    return result


def r5_sign(project: dict[str, Any], *, task_id: str, signed_by: str) -> dict[str, Any]:
    ensure_signature_tasks(project)
    result = mark_signature_signed(project, task_id=task_id, signed_by=signed_by)
    # Re-run preflight after signature
    if result.get("ok"):
        run_preflight(project)
    save_project(project)
    return result


def r5_freeze(project: dict[str, Any]) -> dict[str, Any]:
    result = freeze_submission_package(project)
    if result.get("ok"):
        build_submission_plan(project, dry_run=True)
    save_project(project)
    return result


def r5_submission_plan(project: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    plan = build_submission_plan(project, dry_run=dry_run)
    save_project(project)
    return plan


def r5_dry_run_submit(project: dict[str, Any], *, submitted_by: str = "fixture") -> dict[str, Any]:
    """Fixture-only path. Never contacts external systems."""
    if not project.get("frozen_submission_package"):
        fr = freeze_submission_package(project)
        if not fr.get("ok"):
            return {"ok": False, "error": "cannot_freeze", "detail": fr}
    plan_result = dry_run_submit(project, submitted_by=submitted_by)
    new_submission_event(
        project,
        submitted_by=submitted_by,
        submitted_at=_utc(),
        submission_status=DRY_RUN_SUBMITTED_CONFIRMED,
        dry_run=True,
        notes="TEST SUBMISSION — NOT SENT",
    )
    record_receipt(
        project,
        confirmation_number=f"DRY-{project.get('response_project_id', 'X')[:8]}",
        dry_run=True,
        source="dry_run",
    )
    write_submission_audit(project)
    save_project(project)
    return {**plan_result, "external_side_effects": 0, "sam_api_calls": 0}


def r5_record_receipt(project: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    receipt = record_receipt(project, **kwargs)
    write_submission_audit(project)
    save_project(project)
    return receipt


def invalidate_r5_on_change(project: dict[str, Any], *, reason: str) -> None:
    invalidate_approval(project, reason=reason)
    if project.get("frozen_submission_package"):
        project["frozen_submission_package"]["invalidated"] = True
        project["frozen_submission_package"]["invalidated_reason"] = reason
    if project.get("r5_preflight"):
        project["r5_preflight"]["stale"] = True
        project["r5_preflight"]["stale_reason"] = reason


def r5_operator_card(project: dict[str, Any]) -> dict[str, Any]:
    state = build_operator_state(project)
    pf = project.get("r5_preflight") or {}
    appr = project.get("owner_submission_approval") or {}
    plan = project.get("submission_plan") or {}
    return {
        "build": BUILD,
        "title": "SUBMISSION",
        "plain_status": state.get("plain_status"),
        "next_action": state.get("next_action"),
        "stages": state.get("stages"),
        "preflight": {
            "status": pf.get("overall_status"),
            "summary": f"{pf.get('mandatory_passed', 0)}/{pf.get('mandatory_total', 0)} mandatory checks passed"
            if pf
            else None,
            "warnings": pf.get("warning_count"),
            "fails": pf.get("fail_count"),
            "owner_actions": pf.get("owner_action_count"),
            "next": pf.get("next_action_plain"),
        },
        "approval": {
            "status": appr.get("approval_status"),
            "offer": appr.get("total_offer") or appr.get("selected_price"),
            "expected_profit": appr.get("expected_profit"),
            "profit_state": appr.get("expected_profit_state"),
        },
        "signatures_outstanding": len(
            [t for t in (project.get("signature_tasks") or []) if t.get("status") != "SIGNED"]
        ),
        "auto_signed": auto_signed_count(project),
        "submission_plan": {
            "adapter": plan.get("adapter"),
            "dry_run": plan.get("dry_run"),
            "steps_done": sum(1 for s in (plan.get("steps") or []) if s.get("complete")),
            "steps_total": len(plan.get("steps") or []),
            "label": plan.get("label"),
        }
        if plan
        else None,
        "receipt": project.get("latest_receipt"),
        "submission_status": project.get("r5_submission_status"),
        "never_ready_to_submit": True,
        "external_side_effects": 0,
        "sam_api_calls": 0,
    }


def get_r5_view(response_project_id: str) -> dict[str, Any]:
    project = load_project(response_project_id)
    if not project:
        return {"ok": False, "error": "not_found"}
    return {
        "ok": True,
        "preflight": project.get("r5_preflight"),
        "approval": project.get("owner_submission_approval"),
        "signature_tasks": project.get("signature_tasks") or [],
        "submission_plan": project.get("submission_plan"),
        "freeze": project.get("frozen_submission_package"),
        "receipt": project.get("latest_receipt"),
        "audit": (project.get("submission_audits") or [])[-1:] or None,
        "operator": r5_operator_card(project),
        "operator_state": build_operator_state(project),
        "sam_api_calls": 0,
        "external_side_effects": 0,
    }


__all__ = [
    "run_r5_preflight",
    "r5_owner_approve",
    "r5_sign",
    "r5_freeze",
    "r5_submission_plan",
    "r5_dry_run_submit",
    "r5_record_receipt",
    "invalidate_r5_on_change",
    "r5_operator_card",
    "get_r5_view",
    "mark_step_complete",
    "validate_portal_price_entry",
]
