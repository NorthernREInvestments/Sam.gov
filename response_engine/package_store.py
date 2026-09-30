"""R4 generated package storage — versioned, hashed, original buyer files immutable."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from application_clock import now_utc

from response_engine.models import new_id
from response_engine.r4_constants import BUILD, DRAFT_LABEL

ROOT = Path(__file__).resolve().parents[1]
GENERATED_ROOT = ROOT / "data" / "response_projects"


def _utc() -> str:
    return now_utc().isoformat()


def package_dir(response_project_id: str, version: int) -> Path:
    d = GENERATED_ROOT / response_project_id / "generated" / f"v{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "originals").mkdir(exist_ok=True)
    (d / "buyer_facing").mkdir(exist_ok=True)
    (d / "internal").mkdir(exist_ok=True)
    return d


def next_version(response_project_id: str) -> int:
    base = GENERATED_ROOT / response_project_id / "generated"
    if not base.exists():
        return 1
    existing = [int(p.name[1:]) for p in base.iterdir() if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit()]
    return (max(existing) + 1) if existing else 1


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def new_generated_package(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "GeneratedResponsePackage",
        "package_id": new_id("PKG"),
        "response_project_id": fields.get("response_project_id"),
        "generation_version": fields.get("generation_version") or 1,
        "controlling_document_version": fields.get("controlling_document_version"),
        "r1_version": fields.get("r1_version"),
        "r2_version": fields.get("r2_version"),
        "r3_version": fields.get("r3_version"),
        "created_at": _utc(),
        "created_by": fields.get("created_by") or "r4_service",
        "package_status": fields.get("package_status"),
        "response_type": fields.get("response_type"),
        "submission_system": fields.get("submission_system"),
        "generated_documents": fields.get("generated_documents") or [],
        "buyer_templates": fields.get("buyer_templates") or [],
        "portal_response_dataset": fields.get("portal_response_dataset"),
        "unresolved_fields": fields.get("unresolved_fields") or [],
        "owner_signature_requirements": fields.get("owner_signature_requirements") or [],
        "owner_attestations_pending": fields.get("owner_attestations_pending") or [],
        "material_generation_blockers": fields.get("material_generation_blockers") or [],
        "package_hash_manifest": fields.get("package_hash_manifest") or [],
        "notes": fields.get("notes") or DRAFT_LABEL,
        "build": BUILD,
        "label": DRAFT_LABEL,
        "never_ready_to_submit": True,
    }


def build_manifest(documents: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for d in documents:
        path = d.get("path")
        h = d.get("hash")
        if path and Path(path).exists() and not h:
            h = sha256_file(Path(path))
        entries.append(
            {
                "filename": d.get("filename") or (Path(path).name if path else None),
                "document_type": d.get("document_type"),
                "source_origin": d.get("source_origin"),
                "hash": h,
                "generated_from": d.get("generated_from"),
                "controlling_requirement_ids": d.get("controlling_requirement_ids") or [],
                "owner_signature_required": bool(d.get("owner_signature_required")),
                "owner_attestation_pending": bool(d.get("owner_attestation_pending")),
                "final_status": d.get("status"),
                "buyer_facing": bool(d.get("buyer_facing", True)),
            }
        )
    return {"kind": "ResponsePackageManifest", "entries": entries, "build": BUILD}


def write_internal_manifest(pkg_dir: Path, manifest: dict[str, Any]) -> Path:
    path = pkg_dir / "internal" / "package_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


def assemble_buyer_zip(pkg_dir: Path, buyer_docs: list[dict[str, Any]], zip_name: str = "response_package_DRAFT.zip") -> dict[str, Any]:
    """Allowlist-only ZIP — exclude internal economics, supplier quotes, audit JSON."""
    zpath = pkg_dir / "buyer_facing" / zip_name
    allowed = []
    seen_names: set[str] = set()
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in buyer_docs:
            if not d.get("buyer_facing", True):
                continue
            p = Path(d["path"]) if d.get("path") else None
            if not p or not p.exists():
                continue
            # Refuse internal filenames
            name = p.name.lower()
            if any(x in name for x in ("supplier_quote", "max_buy", "internal", "economics", "debug")):
                continue
            arc = d.get("filename") or p.name
            if arc in seen_names:
                # Filename collision — suffix with document type rather than duplicate zip entries
                stem = Path(arc).stem
                suf = Path(arc).suffix
                arc = f"{stem}_{d.get('document_type') or 'dup'}{suf}"
                if arc in seen_names:
                    continue
            seen_names.add(arc)
            zf.write(p, arcname=arc)
            allowed.append(arc)
    return {
        "zip_path": str(zpath),
        "zip_hash": sha256_file(zpath),
        "files": allowed,
        "label": DRAFT_LABEL,
    }


def new_submission_handoff(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "SubmissionHandoff",
        "handoff_id": new_id("HO"),
        "response_project_id": fields.get("response_project_id"),
        "package_version": fields.get("package_version"),
        "package_id": fields.get("package_id"),
        "clean_buyer_facing_files": fields.get("clean_buyer_facing_files") or [],
        "portal_response_dataset": fields.get("portal_response_dataset"),
        "email_dataset": fields.get("email_dataset"),
        "physical_submission_instructions": fields.get("physical_instructions"),
        "signatures_outstanding": fields.get("signatures_outstanding") or [],
        "final_owner_approvals_outstanding": fields.get("owner_approvals_outstanding") or [],
        "legal_deadline": fields.get("legal_deadline"),
        "internal_safety_deadline": fields.get("internal_safety_deadline"),
        "submission_system": fields.get("submission_system"),
        "package_hashes": fields.get("package_hashes") or [],
        "executed": False,
        "note": "R4 prepares handoff only — R5 executes preflight/submission",
        "build": BUILD,
        "created_at": _utc(),
    }
