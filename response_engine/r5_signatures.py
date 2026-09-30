"""R5 signature tasks — never auto-sign."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r5_constants import (
    BUILD,
    SIG_OWNER_REQUIRED,
    SIG_READY,
    SIG_SIGNED,
    SIG_STALE,
)


def _utc() -> str:
    return now_utc().isoformat()


def ensure_signature_tasks(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Build SignatureTask list from R4 package / handoff. Never marks SIGNED."""
    existing = {t.get("task_id"): t for t in (project.get("signature_tasks") or [])}
    tasks = list(project.get("signature_tasks") or [])
    pkg = project.get("generated_package") or {}
    for d in pkg.get("generated_documents") or []:
        if not d.get("owner_signature_required"):
            continue
        key = f"sig:{d.get('filename') or d.get('path')}"
        if any(t.get("document") == (d.get("filename") or d.get("path")) for t in tasks):
            continue
        tasks.append(
            {
                "kind": "SignatureTask",
                "task_id": new_id("SIG"),
                "document": d.get("filename") or d.get("path"),
                "page": None,
                "field": "signature",
                "signer": "owner",
                "signature_type": "WET_OR_ESIGN",
                "status": SIG_OWNER_REQUIRED,
                "due": project.get("submission_deadline"),
                "instructions": "Owner must sign this document. M3 will not apply a signature automatically.",
                "document_hash": d.get("hash"),
                "build": BUILD,
                "created_at": _utc(),
            }
        )
        _ = key
        _ = existing
    # Always ensure at least a placeholder if package flagged signatures
    if (pkg.get("owner_signature_requirements") or []) and not tasks:
        tasks.append(
            {
                "kind": "SignatureTask",
                "task_id": new_id("SIG"),
                "document": "OFFER_FORMS",
                "field": "signature",
                "signer": "owner",
                "signature_type": "WET_OR_ESIGN",
                "status": SIG_OWNER_REQUIRED,
                "instructions": "Owner signature required on offer forms.",
                "build": BUILD,
                "created_at": _utc(),
            }
        )
    project["signature_tasks"] = tasks
    return tasks


def mark_signature_signed(project: dict[str, Any], *, task_id: str, signed_by: str) -> dict[str, Any]:
    """Explicit owner sign action only — records attestation of signing, does not forge signature bytes."""
    for t in project.get("signature_tasks") or []:
        if t.get("task_id") == task_id:
            # Verify hash still matches if known
            doc_name = t.get("document")
            for d in (project.get("generated_package") or {}).get("generated_documents") or []:
                if (d.get("filename") or d.get("path")) == doc_name and t.get("document_hash") and d.get("hash"):
                    if d.get("hash") != t.get("document_hash"):
                        t["status"] = SIG_STALE
                        return {"ok": False, "error": "document_changed", "task": t}
            t["status"] = SIG_SIGNED
            t["signed_by"] = signed_by
            t["signed_at"] = _utc()
            t["auto_signed"] = False
            return {"ok": True, "task": t}
    return {"ok": False, "error": "task_not_found"}


def invalidate_signatures_for_doc_change(project: dict[str, Any]) -> int:
    n = 0
    for t in project.get("signature_tasks") or []:
        if t.get("status") == SIG_SIGNED:
            t["status"] = SIG_STALE
            t["stale_reason"] = "DOCUMENT_CHANGED"
            n += 1
        elif t.get("status") in (SIG_OWNER_REQUIRED, SIG_READY):
            t["status"] = SIG_OWNER_REQUIRED
    return n


def auto_signed_count(project: dict[str, Any]) -> int:
    return sum(1 for t in (project.get("signature_tasks") or []) if t.get("auto_signed") is True)
