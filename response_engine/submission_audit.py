"""R5 submission events + receipts + immutable audit. Upload ≠ confirmed submission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r5_constants import (
    BUILD,
    DRY_RUN_SUBMITTED_CONFIRMED,
    SUB_SUBMITTED_CONFIRMED,
    SUB_SUBMITTED_UNCONFIRMED,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT_ROOT = ROOT / "data" / "response_projects"


def _utc() -> str:
    return now_utc().isoformat()


def new_submission_event(project: dict[str, Any], **fields: Any) -> dict[str, Any]:
    pkg = project.get("generated_package") or {}
    freeze = project.get("frozen_submission_package") or {}
    event = {
        "kind": "SubmissionEvent",
        "event_id": new_id("SEV"),
        "response_project_id": project.get("response_project_id"),
        "package_version": freeze.get("package_version") or pkg.get("generation_version"),
        "package_id": freeze.get("package_id") or pkg.get("package_id"),
        "adapter": fields.get("adapter") or (project.get("submission_plan") or {}).get("adapter"),
        "submitted_by": fields.get("submitted_by"),
        "started_at": fields.get("started_at") or _utc(),
        "submitted_at": fields.get("submitted_at"),
        "buyer_system": fields.get("buyer_system") or project.get("submission_system"),
        "confirmation_number": fields.get("confirmation_number"),
        "receipt_files": fields.get("receipt_files") or [],
        "package_hashes": freeze.get("package_hashes") or [],
        "submission_status": fields.get("submission_status") or SUB_SUBMITTED_UNCONFIRMED,
        "dry_run": bool(fields.get("dry_run", True)),
        "notes": fields.get("notes"),
        "build": BUILD,
    }
    project.setdefault("submission_events", []).append(event)
    project["latest_submission_event"] = event
    return event


def record_receipt(
    project: dict[str, Any],
    *,
    confirmation_number: str | None = None,
    receipt_path: str | None = None,
    receipt_bytes: bytes | None = None,
    source: str = "manual",
    submitted_timestamp: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Capture receipt. Without confirmation → stays UNCONFIRMED."""
    h = None
    stored_path = None
    if receipt_bytes is not None:
        h = hashlib.sha256(receipt_bytes).hexdigest()
        rid = project.get("response_project_id") or "unknown"
        dest = AUDIT_ROOT / rid / "receipts"
        dest.mkdir(parents=True, exist_ok=True)
        stored_path = dest / f"receipt_{new_id('RCP')}.bin"
        stored_path.write_bytes(receipt_bytes)
    elif receipt_path and Path(receipt_path).exists():
        data = Path(receipt_path).read_bytes()
        h = hashlib.sha256(data).hexdigest()
        stored_path = Path(receipt_path)

    strength = "WEAK"
    status = SUB_SUBMITTED_UNCONFIRMED
    if confirmation_number or h:
        strength = "STRONG" if confirmation_number and h else "MODERATE"
        status = DRY_RUN_SUBMITTED_CONFIRMED if dry_run else SUB_SUBMITTED_CONFIRMED

    receipt = {
        "kind": "SubmissionReceipt",
        "receipt_id": new_id("RCP"),
        "confirmation_number": confirmation_number,
        "submitted_timestamp": submitted_timestamp or _utc(),
        "buyer_system_timestamp": None,
        "solicitation": project.get("solicitation_number") or project.get("title"),
        "offeror": None,
        "submitted_amount": (project.get("owner_submission_approval") or {}).get("total_offer"),
        "files_acknowledged": [],
        "source_artifact": str(stored_path) if stored_path else None,
        "screenshot_or_pdf": str(stored_path) if stored_path else None,
        "hash": h,
        "verification_status": status,
        "evidence_strength": strength,
        "dry_run": dry_run,
        "build": BUILD,
        "captured_at": _utc(),
        "source": source,
    }
    project.setdefault("submission_receipts", []).append(receipt)
    project["latest_receipt"] = receipt

    # Update event
    event = project.get("latest_submission_event")
    if not event:
        event = new_submission_event(
            project,
            submitted_by="operator",
            submitted_at=receipt["submitted_timestamp"],
            confirmation_number=confirmation_number,
            submission_status=status,
            dry_run=dry_run,
        )
    else:
        event["confirmation_number"] = confirmation_number or event.get("confirmation_number")
        event["submitted_at"] = receipt["submitted_timestamp"]
        event["submission_status"] = status
        event.setdefault("receipt_files", []).append(receipt.get("source_artifact"))

    # Mark handoff executed only when confirmed (dry-run still records as dry)
    handoff = project.get("submission_handoff")
    if handoff is not None and status in (SUB_SUBMITTED_CONFIRMED, DRY_RUN_SUBMITTED_CONFIRMED):
        handoff["executed"] = True if not dry_run else False
        handoff["dry_run_executed"] = bool(dry_run)
        handoff["receipt_id"] = receipt["receipt_id"]

    project["r5_submission_status"] = status
    return receipt


def write_submission_audit(project: dict[str, Any]) -> dict[str, Any]:
    """Immutable internal audit snapshot."""
    rid = project.get("response_project_id") or "unknown"
    audit_dir = AUDIT_ROOT / rid / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "kind": "SubmissionAuditPackage",
        "audit_id": new_id("AUD"),
        "response_project_id": rid,
        "solicitation_version": project.get("controlling_document_version"),
        "amendments": project.get("amendments") or [],
        "package_version": (project.get("generated_package") or {}).get("generation_version"),
        "owner_approval": project.get("owner_submission_approval"),
        "signatures": project.get("signature_tasks"),
        "files_hashes": (project.get("frozen_submission_package") or {}).get("package_hashes"),
        "portal_email_fields": (project.get("frozen_submission_package") or {}).get("portal_response_dataset"),
        "submission_event": project.get("latest_submission_event"),
        "receipt": project.get("latest_receipt"),
        "created_at": _utc(),
        "immutable": True,
        "build": BUILD,
    }
    path = audit_dir / f"{audit['audit_id']}.json"
    path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    audit["path"] = str(path)
    project.setdefault("submission_audits", []).append({"audit_id": audit["audit_id"], "path": str(path)})
    return audit
