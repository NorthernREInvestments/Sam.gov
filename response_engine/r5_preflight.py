"""R5 final preflight — deterministic mandatory checks. No vague AI readiness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r5_constants import (
    BUILD,
    FAIL,
    MANUAL_REVIEW_REQUIRED,
    OWNER_ACTION_REQUIRED,
    PASS,
    WARNING,
)


def _utc() -> str:
    return now_utc().isoformat()


def new_preflight_check(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "PreflightCheck",
        "check_id": fields.get("check_id") or new_id("PFC"),
        "category": fields.get("category"),
        "description": fields.get("description"),
        "status": fields.get("status") or PASS,
        "materiality": fields.get("materiality") or "MANDATORY",
        "evidence": fields.get("evidence"),
        "source": fields.get("source"),
        "owner_action_required": bool(fields.get("owner_action_required")),
        "resolution_action": fields.get("resolution_action"),
        "resolved_at": fields.get("resolved_at"),
        "resolved_by": fields.get("resolved_by"),
        "plain": fields.get("plain") or fields.get("description"),
    }


def run_preflight(project: dict[str, Any], *, freeze: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate final package immediately before submission. Fail-closed."""
    checks: list[dict[str, Any]] = []
    pkg = project.get("generated_package") or {}
    handoff = project.get("submission_handoff") or {}
    freeze = freeze or project.get("frozen_submission_package")

    # --- SOLICITATION ---
    checks.append(_chk_docs(project))
    checks.append(_chk_amendments(project, pkg))
    checks.extend(_chk_deadline(project, handoff))
    checks.append(_chk_submission_destination(project, handoff))

    # --- RESPONSE PACKAGE ---
    checks.extend(_chk_package_files(pkg))
    checks.append(_chk_package_stale(pkg, project))

    # --- PRICING ---
    checks.extend(_chk_pricing(project, pkg))

    # --- TECHNICAL ---
    checks.extend(_chk_technical(project))

    # --- COMPANY / COMPLIANCE ---
    checks.extend(_chk_compliance(project))

    # --- SIGNATURES ---
    checks.extend(_chk_signatures(project, pkg, handoff))

    # --- SUBMISSION / FREEZE ---
    checks.append(_chk_firewall(project, pkg))
    if freeze:
        checks.extend(_chk_freeze_integrity(project, freeze))

    mandatory = [c for c in checks if c.get("materiality") == "MANDATORY"]
    passed = sum(1 for c in mandatory if c["status"] == PASS)
    warnings = [c for c in checks if c["status"] == WARNING]
    fails = [c for c in checks if c["status"] == FAIL]
    owner_actions = [c for c in checks if c["status"] == OWNER_ACTION_REQUIRED]

    overall = PASS
    if fails:
        overall = FAIL
    elif owner_actions:
        overall = OWNER_ACTION_REQUIRED
    elif any(c["status"] == MANUAL_REVIEW_REQUIRED for c in checks):
        overall = MANUAL_REVIEW_REQUIRED
    elif warnings:
        overall = WARNING

    next_plain = None
    if owner_actions:
        next_plain = owner_actions[0].get("resolution_action") or owner_actions[0].get("plain")
    elif fails:
        next_plain = fails[0].get("resolution_action") or fails[0].get("plain")
    elif overall in (PASS, WARNING):
        next_plain = "OWNER REVIEW REQUIRED" if not (project.get("owner_submission_approval") or {}).get("approval_status") == "APPROVED" else "READY FOR GUIDED SUBMISSION"

    result = {
        "kind": "PreflightResult",
        "preflight_id": new_id("PF"),
        "build": BUILD,
        "response_project_id": project.get("response_project_id"),
        "package_version": pkg.get("generation_version"),
        "package_id": pkg.get("package_id"),
        "ran_at": _utc(),
        "checks": checks,
        "mandatory_total": len(mandatory),
        "mandatory_passed": passed,
        "warning_count": len(warnings),
        "fail_count": len(fails),
        "owner_action_count": len(owner_actions),
        "overall_status": overall,
        "hard_fails": fails,
        "warnings": warnings,
        "owner_actions": owner_actions,
        "next_action_plain": next_plain,
        "approval_eligible": overall in (PASS, WARNING) and not fails and not owner_actions,
        "LIVE_API_REQUESTS": 0,
        "sam_api_calls": 0,
        "external_side_effects": 0,
    }
    project["r5_preflight"] = result
    project["r5_preflight_at"] = _utc()
    return result


def _chk_docs(project: dict[str, Any]) -> dict[str, Any]:
    docs = project.get("documents") or []
    if not docs and not (project.get("requirements") or []):
        return new_preflight_check(
            category="SOLICITATION",
            description="Solicitation package loaded",
            status=FAIL,
            plain="Solicitation documents missing",
            resolution_action="LOAD SOLICITATION",
        )
    return new_preflight_check(
        category="SOLICITATION",
        description="Solicitation package loaded",
        status=PASS,
        plain="Solicitation documents present",
        evidence=f"{len(docs)} documents",
    )


def _chk_amendments(project: dict[str, Any], pkg: dict[str, Any]) -> dict[str, Any]:
    if project.get("package_stale") or (pkg.get("package_status") or "").startswith("STALE"):
        return new_preflight_check(
            category="SOLICITATION",
            description="Amendments current vs package",
            status=FAIL,
            plain="Response package is stale — amendment or source change detected",
            resolution_action="REVIEW AMENDMENT",
        )
    if project.get("amendment_review_queue"):
        return new_preflight_check(
            category="SOLICITATION",
            description="Amendment acknowledgments",
            status=OWNER_ACTION_REQUIRED,
            owner_action_required=True,
            plain="Amendment needs review / acknowledgment",
            resolution_action="REVIEW AMENDMENT",
        )
    return new_preflight_check(
        category="SOLICITATION",
        description="Amendments current vs package",
        status=PASS,
        plain="No unresolved amendment queue",
    )


def _chk_deadline(project: dict[str, Any], handoff: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    raw = handoff.get("legal_deadline") or project.get("submission_deadline")
    tz_name = project.get("submission_timezone") or "America/Chicago"
    if not raw:
        out.append(
            new_preflight_check(
                category="SOLICITATION",
                description="Response due date/time known",
                status=WARNING,
                materiality="ADVISORY",
                plain="Bid deadline not clearly extracted — confirm before submit",
                resolution_action="CONFIRM DEADLINE",
            )
        )
        return out
    out.append(
        new_preflight_check(
            category="SOLICITATION",
            description="Response due date/time known",
            status=PASS,
            plain=f"Deadline recorded: {raw} ({tz_name})",
            evidence={"deadline": raw, "timezone": tz_name, "source": "project/handoff"},
        )
    )
    parsed = _parse_deadline(raw, tz_name)
    if parsed is None:
        out.append(
            new_preflight_check(
                category="SOLICITATION",
                description="Deadline parseable with timezone",
                status=MANUAL_REVIEW_REQUIRED,
                plain="Could not parse exact deadline time — manual confirm",
                resolution_action="CONFIRM DEADLINE",
            )
        )
        return out
    now = now_utc()
    if now > parsed:
        out.append(
            new_preflight_check(
                category="SOLICITATION",
                description="Still before controlling deadline",
                status=FAIL,
                plain="SUBMISSION DEADLINE PASSED",
                resolution_action="DO NOT SUBMIT — DEADLINE PASSED",
                evidence={"now": now.isoformat(), "deadline": parsed.isoformat()},
            )
        )
    else:
        hours = (parsed - now).total_seconds() / 3600
        urgency = WARNING if hours < 4 else PASS
        out.append(
            new_preflight_check(
                category="SOLICITATION",
                description="Still before controlling deadline",
                status=urgency,
                materiality="MANDATORY" if urgency == PASS else "ADVISORY",
                plain=f"Deadline in ~{hours:.1f}h ({tz_name})",
                evidence={"hours_remaining": round(hours, 2)},
            )
        )
    # Internal safety target (advisory)
    internal = handoff.get("internal_safety_deadline") or project.get("internal_submission_target")
    if internal:
        out.append(
            new_preflight_check(
                category="SOLICITATION",
                description="Internal submission target",
                status=PASS,
                materiality="ADVISORY",
                plain=f"Internal target: {internal}",
            )
        )
    return out


def _chk_submission_destination(project: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    dest = handoff.get("submission_system") or project.get("submission_system") or project.get("portal")
    if not dest:
        return new_preflight_check(
            category="SUBMISSION",
            description="Submission destination known",
            status=WARNING,
            materiality="ADVISORY",
            plain="Submission destination not clearly set — confirm portal/email/address",
            resolution_action="CONFIRM SUBMISSION METHOD",
        )
    return new_preflight_check(
        category="SUBMISSION",
        description="Submission destination known",
        status=PASS,
        plain=f"Destination: {dest}",
        evidence=dest,
    )


def _chk_package_files(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    docs = pkg.get("generated_documents") or []
    if not docs:
        out.append(
            new_preflight_check(
                category="RESPONSE_PACKAGE",
                description="Required response files present",
                status=FAIL,
                plain="No generated response documents — build response package first",
                resolution_action="BUILD RESPONSE PACKAGE",
            )
        )
        return out
    missing = []
    for d in docs:
        if not d.get("buyer_facing", True):
            continue
        path = d.get("path")
        if not path or not Path(path).exists():
            missing.append(d.get("filename") or path)
            continue
        p = Path(path)
        if p.stat().st_size == 0:
            missing.append(f"{p.name} (empty)")
    if missing:
        out.append(
            new_preflight_check(
                category="RESPONSE_PACKAGE",
                description="Required response files present and non-empty",
                status=FAIL,
                plain=f"Missing/empty files: {', '.join(str(m) for m in missing[:5])}",
                resolution_action="REBUILD RESPONSE PACKAGE",
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="RESPONSE_PACKAGE",
                description="Required response files present and non-empty",
                status=PASS,
                plain=f"{len(docs)} generated documents present",
            )
        )
    # Hash manifest
    hashes = pkg.get("package_hash_manifest") or []
    out.append(
        new_preflight_check(
            category="RESPONSE_PACKAGE",
            description="Package hash manifest present",
            status=PASS if hashes else WARNING,
            materiality="ADVISORY" if not hashes else "MANDATORY",
            plain="Hash manifest present" if hashes else "Hash manifest missing",
        )
    )
    return out


def _chk_package_stale(pkg: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    if pkg.get("stale") or (pkg.get("package_status") or "").startswith("STALE"):
        return new_preflight_check(
            category="RESPONSE_PACKAGE",
            description="Package not stale",
            status=FAIL,
            plain="Package stale — rebuild before submission",
            resolution_action="BUILD RESPONSE PACKAGE",
        )
    return new_preflight_check(
        category="RESPONSE_PACKAGE",
        description="Package not stale",
        status=PASS,
        plain="Package not marked stale",
    )


def _chk_pricing(project: dict[str, Any], pkg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    issues = pkg.get("total_validation_issues") or []
    if issues:
        out.append(
            new_preflight_check(
                category="PRICING",
                description="Line/extended totals consistent",
                status=FAIL,
                plain="Pricing total mismatch detected",
                resolution_action="FIX PRICING TOTALS",
                evidence=issues[:3],
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="PRICING",
                description="Line/extended totals consistent",
                status=PASS,
                plain="No total mismatches recorded",
            )
        )
    sel = project.get("selected_bid_price_scenario") or {}
    if not sel.get("scenario_id") and not pkg.get("selected_scenario_id"):
        # Not always required for incomplete drafts — warn
        out.append(
            new_preflight_check(
                category="PRICING",
                description="Bid price scenario selected for draft",
                status=WARNING,
                materiality="ADVISORY",
                plain="No price scenario selected — confirm offer price before submit",
                resolution_action="SELECT PRICE SCENARIO",
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="PRICING",
                description="Bid price scenario selected for draft",
                status=PASS,
                plain="Price scenario selected for package",
            )
        )
    return out


def _chk_technical(project: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    tech = project.get("technical_compliance_items") or []
    fails = [t for t in tech if t.get("status") == "FAIL"]
    if fails:
        out.append(
            new_preflight_check(
                category="TECHNICAL",
                description="No known technical FAIL",
                status=FAIL,
                plain="DO NOT BID — PRODUCT DOES NOT MEET REQUIREMENT",
                resolution_action="RESOLVE TECHNICAL FAIL OR DO NOT BID",
                evidence=fails[:2],
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="TECHNICAL",
                description="No known technical FAIL",
                status=PASS,
                plain="No technical FAIL items recorded",
            )
        )
    return out


def _chk_compliance(project: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    r3 = project.get("r3_analysis") or {}
    rec = project.get("r3_recommendation") or r3.get("recommendation")
    if rec == "DO_NOT_BID" or (project.get("r3_readiness") or "").endswith("BLOCKED"):
        out.append(
            new_preflight_check(
                category="COMPLIANCE",
                description="Company/product eligibility",
                status=FAIL,
                plain="DO NOT BID — COMPANY/PRODUCT NOT ELIGIBLE",
                resolution_action="RESOLVE COMPLIANCE OR DO NOT BID",
                evidence=project.get("r3_blockers") or r3.get("blockers"),
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="COMPLIANCE",
                description="Company/product eligibility",
                status=PASS if not (project.get("r3_blockers") or []) else WARNING,
                materiality="ADVISORY" if project.get("r3_blockers") else "MANDATORY",
                plain="No hard DO_NOT_BID from compliance" if not project.get("r3_blockers") else "Compliance warnings present",
            )
        )
    # Owner attestations
    open_att = [a for a in (project.get("owner_attestations") or []) if not a.get("owner_confirmed")]
    if open_att:
        out.append(
            new_preflight_check(
                category="COMPLIANCE",
                description="Required owner attestations confirmed",
                status=OWNER_ACTION_REQUIRED,
                owner_action_required=True,
                plain=f"Need owner confirmation ({len(open_att)})",
                resolution_action="CONFIRM OWNER ATTESTATIONS",
            )
        )
    else:
        out.append(
            new_preflight_check(
                category="COMPLIANCE",
                description="Required owner attestations confirmed",
                status=PASS,
                plain="No open owner attestations",
            )
        )
    return out


def _chk_signatures(project: dict[str, Any], pkg: dict[str, Any], handoff: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = project.get("signature_tasks") or []
    outstanding = [t for t in tasks if t.get("status") in ("OWNER_SIGNATURE_REQUIRED", "READY_FOR_SIGNATURE", "STALE")]
    # Also from package
    if not tasks and (pkg.get("owner_signature_requirements") or handoff.get("signatures_outstanding")):
        outstanding = pkg.get("owner_signature_requirements") or handoff.get("signatures_outstanding") or []
    if outstanding:
        n = len(outstanding)
        return [
            new_preflight_check(
                category="SIGNATURES",
                description="Required signatures executed",
                status=OWNER_ACTION_REQUIRED,
                owner_action_required=True,
                plain=f"{n} document(s) need your signature",
                resolution_action="SIGN DOCUMENTS",
            )
        ]
    return [
        new_preflight_check(
            category="SIGNATURES",
            description="Required signatures executed",
            status=PASS,
            plain="No outstanding signature tasks (or none required yet)",
        )
    ]


def _chk_firewall(project: dict[str, Any], pkg: dict[str, Any]) -> dict[str, Any]:
    fw = project.get("r4_firewall") or {}
    leaks = list(fw.get("leaks") or []) + list(pkg.get("firewall_leaks") or [])
    if leaks:
        return new_preflight_check(
            category="SUBMISSION",
            description="Buyer package free of internal economics",
            status=FAIL,
            plain="Internal data found in buyer-facing package",
            resolution_action="REMOVE INTERNAL LEAKS",
            evidence=leaks,
        )
    return new_preflight_check(
        category="SUBMISSION",
        description="Buyer package free of internal economics",
        status=PASS,
        plain="No firewall leaks recorded",
    )


def _chk_freeze_integrity(project: dict[str, Any], freeze: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    # Hash match
    for entry in freeze.get("package_hashes") or []:
        path = entry.get("path")
        expected = entry.get("hash")
        if path and expected and Path(path).exists():
            import hashlib

            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if actual != expected:
                out.append(
                    new_preflight_check(
                        category="RESPONSE_PACKAGE",
                        description="Approved file hashes unchanged",
                        status=FAIL,
                        plain="APPROVED FILE CHANGED — reapproval required",
                        resolution_action="REBUILD AND REAPPROVE",
                        evidence={"file": entry.get("filename"), "expected": expected, "actual": actual},
                    )
                )
                return out
    out.append(
        new_preflight_check(
            category="RESPONSE_PACKAGE",
            description="Approved file hashes unchanged",
            status=PASS,
            plain="Frozen package hashes match current files",
        )
    )
    return out


def _parse_deadline(raw: Any, tz_name: str) -> datetime | None:
    """Best-effort parse; fail-closed returns None → manual review."""
    text = str(raw).strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    # ISO
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # Common US patterns
    import re

    m = re.search(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?:\s+(\d{1,2}):(\d{2})\s*(AM|PM)?)?",
        text,
        re.I,
    )
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    hour = int(m.group(4) or 23)
    minute = int(m.group(5) or 59)
    ampm = (m.group(6) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
    except Exception:
        return None
