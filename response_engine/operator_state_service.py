"""Canonical operator-facing state — plain language, one next action. Hides R1–R5 jargon."""

from __future__ import annotations

from typing import Any

from response_engine.r5_constants import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    BUILD,
    DRY_RUN_SUBMITTED_CONFIRMED,
    FAIL,
    MANUAL_REVIEW_REQUIRED,
    OWNER_ACTION_REQUIRED,
    ROLE_OPERATOR,
    ROLE_OWNER,
    STATUS_AWAITING_RESULT,
    STATUS_BUILD_RESPONSE,
    STATUS_DEADLINE_PASSED,
    STATUS_DO_NOT_BID,
    STATUS_NEEDS_ACTION,
    STATUS_READY_FOR_APPROVAL,
    STATUS_READY_TO_SUBMIT,
    STATUS_SUBMITTED,
    STATUS_WAITING_EXTERNAL,
    STATUS_WAITING_OWNER,
    SUB_SUBMITTED_CONFIRMED,
    SUB_SUBMITTED_UNCONFIRMED,
)


def build_operator_state(project: dict[str, Any] | None, *, base_card: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Single presenter for Deal / Bid Prep / Today.
    Returns plain status, next action, role, destination — no R1/R2/R3/R4/R5 labels in normal UI.
    """
    card = dict(base_card or {})
    if not project:
        return {
            **card,
            "build": BUILD,
            "plain_status": STATUS_NEEDS_ACTION,
            "next_action": _nba("START_BID_PREP", "START BID PREP", "Response project not started", ROLE_OPERATOR, "bid-prep", True),
            "stages": _empty_stages(),
            "owner_actions": [],
            "operator_actions": [],
            "never_ready_to_submit": True,
        }

    # Precedence ladder
    next_action = None
    plain_status = STATUS_NEEDS_ACTION
    stages = _compute_stages(project)

    # 1. Deadline passed
    pf = project.get("r5_preflight") or {}
    for c in pf.get("hard_fails") or []:
        if "DEADLINE" in (c.get("plain") or "").upper():
            plain_status = STATUS_DEADLINE_PASSED
            next_action = _nba("DEADLINE_PASSED", "DEADLINE PASSED", c.get("plain"), ROLE_OWNER, "deal", True)
            break

    # 2. Hard eligibility / technical / do not bid
    if not next_action and (
        project.get("r3_recommendation") == "DO_NOT_BID"
        or (project.get("r2_recommendation") or "") == "DO_NOT_BID"
    ):
        reason = (project.get("r3_blockers") or project.get("r2_blockers") or ["Not eligible"])[0]
        plain_status = STATUS_DO_NOT_BID
        next_action = _nba("DO_NOT_BID", "DO NOT BID", _plain_blocker(reason), ROLE_OWNER, "deal", True)

    # Tech fail
    if not next_action:
        tech_fails = [t for t in (project.get("technical_compliance_items") or []) if t.get("status") == "FAIL"]
        if tech_fails or project.get("r2_readiness") == "TECHNICAL_FAIL":
            plain_status = STATUS_DO_NOT_BID
            next_action = _nba(
                "TECHNICAL_FAIL",
                "DO NOT BID — PRODUCT ISSUE",
                "Product does not meet a required specification",
                ROLE_OPERATOR,
                "deal",
                True,
            )

    # 3. Submission status
    sub_status = project.get("r5_submission_status")
    if not next_action and sub_status in (SUB_SUBMITTED_CONFIRMED, DRY_RUN_SUBMITTED_CONFIRMED):
        plain_status = STATUS_AWAITING_RESULT
        next_action = _nba("AWAITING_RESULT", "AWAITING RESULT", "Submission confirmed — waiting on buyer", ROLE_OPERATOR, "deal", False)
    elif not next_action and sub_status == SUB_SUBMITTED_UNCONFIRMED:
        plain_status = STATUS_NEEDS_ACTION
        next_action = _nba("VERIFY_RECEIPT", "VERIFY SUBMISSION NOW", "Submission attempted but no confirmation yet", ROLE_OWNER, "bid-prep", True)

    # 4. CAGE / registration style blocks from R3
    if not next_action:
        for b in project.get("r3_blockers") or []:
            plain = _plain_blocker(b)
            if "CAGE" in str(b).upper():
                plain_status = STATUS_WAITING_EXTERNAL
                next_action = _nba("RESOLVE_CAGE", "RESOLVE CAGE", plain, ROLE_OWNER, "settings", True)
                break
            if "REGISTER" in str(b).upper() or "REGISTRATION" in str(b).upper():
                plain_status = STATUS_NEEDS_ACTION
                next_action = _nba("REGISTER", "COMPLETE REGISTRATION", plain, ROLE_OPERATOR, "registrations", True)
                break

    # 5. Owner attestations
    if not next_action:
        open_att = [a for a in (project.get("owner_attestations") or []) if not a.get("owner_confirmed")]
        if open_att:
            plain_status = STATUS_WAITING_OWNER
            next_action = _nba(
                "OWNER_ATTESTATION",
                "OWNER CONFIRMATION REQUIRED",
                (open_att[0].get("question") or "Owner confirmation needed")[:120],
                ROLE_OWNER,
                "bid-prep",
                True,
            )

    # 6. Signatures
    if not next_action:
        sigs = [t for t in (project.get("signature_tasks") or []) if t.get("status") in ("OWNER_SIGNATURE_REQUIRED", "READY_FOR_SIGNATURE", "STALE")]
        if sigs:
            plain_status = STATUS_WAITING_OWNER
            next_action = _nba("SIGN", f"SIGN {len(sigs)} DOCUMENT(S)", "Documents need your signature", ROLE_OWNER, "bid-prep", True)

    # 7. Preflight / approval / submit
    approval = project.get("owner_submission_approval") or {}
    if not next_action and approval.get("approval_status") == APPROVAL_APPROVED and project.get("frozen_submission_package"):
        plain_status = STATUS_READY_TO_SUBMIT
        next_action = _nba("SUBMIT", "SUBMIT IN PORTAL", "Owner approved — follow guided submission steps", ROLE_OPERATOR, "bid-prep", True)
    elif not next_action and pf.get("approval_eligible") and approval.get("approval_status") != APPROVAL_APPROVED:
        plain_status = STATUS_READY_FOR_APPROVAL
        next_action = _nba("OWNER_APPROVE", "OWNER REVIEW REQUIRED", "Preflight passed — ready for your approval", ROLE_OWNER, "bid-prep", True)
    elif not next_action and (project.get("generated_package") or {}).get("package_id"):
        overall = pf.get("overall_status")
        if not pf:
            plain_status = STATUS_NEEDS_ACTION
            next_action = _nba("PREFLIGHT", "RUN FINAL PREFLIGHT", "Response package built — run final checks", ROLE_OPERATOR, "bid-prep", True)
        elif overall in (FAIL, OWNER_ACTION_REQUIRED, MANUAL_REVIEW_REQUIRED) or not pf.get("approval_eligible"):
            plain_status = STATUS_NEEDS_ACTION
            next_action = _nba(
                "FIX_PREFLIGHT",
                pf.get("next_action_plain") or "FIX PREFLIGHT ISSUES",
                pf.get("next_action_plain") or "Resolve preflight blockers before approval",
                ROLE_OWNER if pf.get("owner_action_count") else ROLE_OPERATOR,
                "bid-prep",
                True,
            )
    elif not next_action and project.get("documents"):
        # Have solicitation but no package
        r4 = project.get("r4_package_status")
        if not r4 or r4 == "NOT_STARTED":
            plain_status = STATUS_BUILD_RESPONSE
            next_action = _nba("BUILD_RESPONSE", "BUILD RESPONSE PACKAGE", "Enough data to draft response", ROLE_OPERATOR, "bid-prep", True)
        else:
            plain_status = STATUS_NEEDS_ACTION
            next_action = _nba("BUILD_RESPONSE", "BUILD RESPONSE PACKAGE", project.get("r4_next_action") or "Continue response", ROLE_OPERATOR, "bid-prep", True)
    elif not next_action:
        plain_status = STATUS_NEEDS_ACTION
        next_action = _nba("START_BID_PREP", "START BID PREP", "Load solicitation package", ROLE_OPERATOR, "bid-prep", True)

    owner_actions = []
    operator_actions = []
    if next_action:
        if next_action["assigned_role"] == ROLE_OWNER:
            owner_actions.append(next_action)
        else:
            operator_actions.append(next_action)

    # Extra owner items
    for a in project.get("owner_attestations") or []:
        if not a.get("owner_confirmed"):
            owner_actions.append(
                _nba("OWNER_ATTESTATION", "CONFIRM ATTESTATION", (a.get("question") or "")[:80], ROLE_OWNER, "bid-prep", True)
            )

    return {
        **card,
        "build": BUILD,
        "response_project_id": project.get("response_project_id"),
        "plain_status": plain_status,
        "next_action": next_action,
        "ui_next_action": (next_action or {}).get("action_type"),
        "ui_next_action_label": (next_action or {}).get("action_label"),
        "ui_next_action_reason": (next_action or {}).get("reason"),
        "stages": stages,
        "owner_actions": owner_actions[:10],
        "operator_actions": operator_actions[:10],
        "preflight": {
            "status": pf.get("overall_status"),
            "passed": pf.get("mandatory_passed"),
            "total": pf.get("mandatory_total"),
            "warnings": pf.get("warning_count"),
            "fails": pf.get("fail_count"),
            "owner_actions": pf.get("owner_action_count"),
        }
        if pf
        else None,
        "approval_status": approval.get("approval_status"),
        "submission_status": sub_status,
        "deadline": project.get("submission_deadline"),
        "timezone": project.get("submission_timezone"),
        "never_ready_to_submit": sub_status not in (SUB_SUBMITTED_CONFIRMED,),
        "label_draft": "DRAFT — NOT SUBMITTED" if sub_status not in (SUB_SUBMITTED_CONFIRMED, DRY_RUN_SUBMITTED_CONFIRMED) else None,
    }


def _nba(action_type: str, label: str, reason: str, role: str, destination: str, blocking: bool) -> dict[str, Any]:
    return {
        "kind": "NextBestAction",
        "action_type": action_type,
        "action_label": label,
        "reason": reason,
        "priority": 1 if blocking else 5,
        "assigned_role": role,
        "destination": destination,
        "blocking": blocking,
    }


def _plain_blocker(raw: Any) -> str:
    s = str(raw or "")
    mapping = [
        ("CAGE", "CAGE required before this federal/DIBBS path can proceed"),
        ("DIBBS", "DIBBS unavailable until CAGE is active"),
        ("889", "Need owner confirmation on telecommunications representation"),
        ("NMR", "Nonmanufacturer rule needs review"),
        ("COUNTRY", "Need country-of-origin proof"),
        ("ORIGIN", "Need country-of-origin proof"),
        ("TRADE", "Need trade-compliance / origin evidence"),
        ("UOM", "Confirm how buyer units map to supplier pack size"),
        ("QUOTE", "Get a supplier quote"),
        ("REGISTER", "Complete vendor registration before bid"),
        ("ATTESTATION", "Owner confirmation required"),
        ("SIGNATURE", "Documents need your signature"),
    ]
    up = s.upper()
    for key, plain in mapping:
        if key in up:
            return plain
    # Strip internal codes
    return s.replace("OWNER_CONFIRMATION_REQUIRED", "Owner confirmation required").replace("_", " ")[:160]


def _compute_stages(project: dict[str, Any]) -> list[dict[str, Any]]:
    def st(name: str, state: str) -> dict[str, Any]:
        return {"id": name.lower(), "label": name, "state": state}

    stages = []
    stages.append(st("Opportunity", "COMPLETE" if project.get("documents") else "NEEDS ACTION"))
    quotes = project.get("supplier_quotes") or []
    stages.append(st("Supplier", "COMPLETE" if quotes else ("NEEDS ACTION" if project.get("documents") else "NOT STARTED")))
    products = project.get("offered_products") or []
    tech_fail = any(t.get("status") == "FAIL" for t in (project.get("technical_compliance_items") or []))
    stages.append(st("Product", "BLOCKED" if tech_fail else ("COMPLETE" if products else "NOT STARTED")))
    r3 = project.get("r3_readiness")
    stages.append(
        st(
            "Compliance",
            "BLOCKED"
            if project.get("r3_recommendation") == "DO_NOT_BID"
            else ("COMPLETE" if r3 in ("COMPLIANCE_READY", "READY_FOR_RESPONSE_BUILD") else ("NEEDS ACTION" if r3 else "NOT STARTED")),
        )
    )
    pkg = project.get("generated_package")
    stages.append(st("Response", "COMPLETE" if pkg and not pkg.get("stale") else ("NEEDS ACTION" if project.get("documents") else "NOT STARTED")))
    appr = (project.get("owner_submission_approval") or {}).get("approval_status")
    stages.append(
        st(
            "Approval",
            "COMPLETE"
            if appr == APPROVAL_APPROVED
            else ("NEEDS ACTION" if appr == APPROVAL_PENDING or (project.get("r5_preflight") or {}).get("approval_eligible") else "NOT STARTED"),
        )
    )
    sub = project.get("r5_submission_status")
    stages.append(
        st(
            "Submission",
            "COMPLETE"
            if sub in (SUB_SUBMITTED_CONFIRMED, DRY_RUN_SUBMITTED_CONFIRMED)
            else ("NEEDS ACTION" if appr == APPROVAL_APPROVED else "NOT STARTED"),
        )
    )
    stages.append(
        st("Result", "WAITING" if sub in (SUB_SUBMITTED_CONFIRMED, DRY_RUN_SUBMITTED_CONFIRMED) else "NOT STARTED")
    )
    return stages


def _empty_stages() -> list[dict[str, Any]]:
    return [
        {"id": s.lower(), "label": s, "state": "NOT STARTED"}
        for s in ("Opportunity", "Supplier", "Product", "Compliance", "Response", "Approval", "Submission", "Result")
    ]
