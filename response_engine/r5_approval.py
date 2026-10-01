"""R5 owner approval — package-version specific. Explicit human action only."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r5_constants import (
    APPROVAL_APPROVED,
    APPROVAL_CHANGES_REQUESTED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    BUILD,
)


def _utc() -> str:
    return now_utc().isoformat()


def create_approval_request(project: dict[str, Any], *, preflight: dict[str, Any]) -> dict[str, Any]:
    pkg = project.get("generated_package") or {}
    scenario = project.get("selected_bid_price_scenario") or {}
    scenarios = project.get("pricing_scenarios") or []
    sel = next((s for s in scenarios if s.get("scenario_id") == (scenario.get("scenario_id") or pkg.get("selected_scenario_id"))), None)
    approval = {
        "kind": "OwnerSubmissionApproval",
        "approval_id": new_id("OAP"),
        "response_project_id": project.get("response_project_id"),
        "package_version": pkg.get("generation_version"),
        "package_id": pkg.get("package_id"),
        "selected_price": (sel or {}).get("total_bid_price") or scenario.get("total_bid_price"),
        "total_offer": (sel or {}).get("total_bid_price"),
        "expected_profit": (sel or {}).get("expected_profit"),
        "expected_profit_state": (sel or {}).get("evidence_quality") or "SCENARIO",
        "margin": (sel or {}).get("margin"),
        "key_warnings": [w.get("plain") for w in (preflight.get("warnings") or [])],
        "unresolved_permitted_warnings": [w.get("check_id") for w in (preflight.get("warnings") or [])],
        "preflight_id": preflight.get("preflight_id"),
        "approval_status": APPROVAL_PENDING,
        "approved_by": None,
        "approved_at": None,
        "created_at": _utc(),
        "build": BUILD,
        "plain_summary": {
            "buyer": project.get("buyer"),
            "solicitation": project.get("solicitation_number") or project.get("title"),
            "due": project.get("submission_deadline"),
            "timezone": project.get("submission_timezone"),
            "documents": len(pkg.get("generated_documents") or []),
            "submission": project.get("submission_system") or project.get("portal"),
        },
    }
    project["owner_submission_approval"] = approval
    return approval


def decide_owner_approval(
    project: dict[str, Any],
    *,
    decision: str,
    approved_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """decision: APPROVED | REJECTED | CHANGES_REQUESTED"""
    approval = project.get("owner_submission_approval")
    if not approval:
        return {"ok": False, "error": "no_approval_request"}
    pkg = project.get("generated_package") or {}
    if approval.get("package_id") != pkg.get("package_id") or approval.get("package_version") != pkg.get("generation_version"):
        approval["approval_status"] = APPROVAL_EXPIRED
        return {"ok": False, "error": "package_changed", "approval": approval}

    decision_u = decision.upper()
    if decision_u not in {APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_CHANGES_REQUESTED, "APPROVED", "REJECTED", "CHANGES_REQUESTED"}:
        return {"ok": False, "error": "invalid_decision"}
    # Normalize
    if decision_u == "APPROVED":
        decision_u = APPROVAL_APPROVED
    elif decision_u == "REJECTED":
        decision_u = APPROVAL_REJECTED
    elif decision_u == "CHANGES_REQUESTED":
        decision_u = APPROVAL_CHANGES_REQUESTED

    pf = project.get("r5_preflight") or {}
    if decision_u == APPROVAL_APPROVED and not pf.get("approval_eligible"):
        return {"ok": False, "error": "preflight_not_eligible", "preflight": pf.get("overall_status")}

    approval["approval_status"] = decision_u
    approval["approved_by"] = approved_by
    approval["approved_at"] = _utc()
    approval["decision_reason"] = reason
    project["owner_submission_approval"] = approval
    project.setdefault("approval_history", []).append(dict(approval))
    return {"ok": True, "approval": approval}


def invalidate_approval(project: dict[str, Any], *, reason: str) -> None:
    approval = project.get("owner_submission_approval")
    if not approval:
        return
    if approval.get("approval_status") == APPROVAL_APPROVED:
        approval["approval_status"] = APPROVAL_EXPIRED
        approval["expired_reason"] = reason
        approval["expired_at"] = _utc()
        project["owner_submission_approval"] = approval


def freeze_submission_package(project: dict[str, Any]) -> dict[str, Any]:
    """Freeze approved package bytes/hashes — does NOT submit."""
    approval = project.get("owner_submission_approval") or {}
    if approval.get("approval_status") != APPROVAL_APPROVED:
        return {"ok": False, "error": "not_approved"}
    pkg = project.get("generated_package") or {}
    approved_pkg = approval.get("package_id")
    current_pkg = pkg.get("package_id")
    if approved_pkg and current_pkg and approved_pkg != current_pkg:
        return {
            "ok": False,
            "error": "approval_package_mismatch",
            "detail": "Owner approved a different package version — re-run preflight and approve the current package.",
            "approved_package_id": approved_pkg,
            "current_package_id": current_pkg,
        }
    if not current_pkg:
        return {"ok": False, "error": "no_generated_package"}
    handoff = project.get("submission_handoff") or {}
    freeze = {
        "kind": "FrozenSubmissionPackage",
        "freeze_id": new_id("FRZ"),
        "response_project_id": project.get("response_project_id"),
        "package_version": pkg.get("generation_version"),
        "package_id": pkg.get("package_id"),
        "approval_id": approval.get("approval_id"),
        "selected_price": approval.get("selected_price") or approval.get("total_offer"),
        "files": [
            {"filename": d.get("filename"), "path": d.get("path"), "hash": d.get("hash")}
            for d in (pkg.get("generated_documents") or [])
            if d.get("buyer_facing", True)
        ],
        "package_hashes": handoff.get("package_hashes") or pkg.get("package_hash_manifest") or [],
        "portal_response_dataset": handoff.get("portal_response_dataset") or pkg.get("portal_response_dataset"),
        "email_dataset": handoff.get("email_dataset"),
        "physical_instructions": handoff.get("physical_submission_instructions"),
        "frozen_at": _utc(),
        "submitted": False,
        "build": BUILD,
    }
    project["frozen_submission_package"] = freeze
    return {"ok": True, "freeze": freeze}
