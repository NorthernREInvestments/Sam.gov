"""Canonical solicitation ↔ document association (offline; zero API).

Builds one logical manifest per opportunity from retained NextBid responses,
evidence vault, full-text store, and content-addressed local checks.

``stored=true`` on NextBid ``source_documents`` means the provider object store
holds the original — it does **not** prove M3 has PDF bytes locally.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.full_text_store import load_full_text
from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_offline_recovery import load_recovery_package
from m3.research.nextbid_response_store import (
    load_by_request_id,
    lookup_cached_response,
    request_identity,
    store_root,
)

MANIFEST_VERSION = 1


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_root(root: Path | None = None) -> Path:
    p = (root or get_settings().data_root()) / "solicitation_document_manifests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def manifest_path(nb_id: str, *, root: Path | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._\-]+", "_", nb_id)[:80]
    return manifest_root(root) / f"{safe}.json"


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _iter_nb_paid_rows(nb_id: str) -> list[dict[str, Any]]:
    idx = store_root() / "by_nb_id" / f"{nb_id}.jsonl"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not idx.exists():
        return rows
    for line in idx.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            meta = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = str(meta.get("request_id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        full = load_by_request_id(rid)
        if full:
            rows.append(full)
    return rows


def local_pdf_bytes_by_sha(sha256: str | None) -> dict[str, Any]:
    """Content-addressed local PDF check (evidence vault + common data roots)."""
    if not sha256:
        return {"present": False, "path": None, "bytes": None}
    from m3.research.evidence_vault import get_body, vault_root

    body = get_body(sha256)
    if body:
        return {"present": True, "path": f"evidence_vault:blobs/{sha256[:2]}/{sha256}.gz", "bytes": len(body)}
    # Direct blob path (may exist even if get_body fails)
    bp = vault_root() / "blobs" / sha256[:2] / f"{sha256}.gz"
    if bp.exists():
        return {"present": True, "path": str(bp), "bytes": bp.stat().st_size}
    return {"present": False, "path": None, "bytes": None}


def file_artifact_retention(nb_id: str, relative_path: str) -> dict[str, Any]:
    """Whether a NextBid ``files/<path>`` artifact is already durably retained locally."""
    http_path = f"/opportunities/{nb_id}/files/{relative_path}"
    rid = request_identity("GET", http_path, None, nb_id=nb_id)
    row = load_by_request_id(rid)
    durable_body = lookup_cached_response("GET", http_path, None, nb_id=nb_id)
    text = nextbid_file_text(durable_body) if durable_body is not None else ""
    if not text and row:
        text = nextbid_file_text(row.get("body"))
    ft = load_full_text(nb_id) or {}
    ft_text = str(ft.get("full_text") or "")
    src = str(ft.get("source") or "")
    ft_covers = bool(
        relative_path.endswith(".md")
        and len(ft_text) >= 800
        and (
            src in {
                "nextbid_pipeline_read_file",
                "nextbid_offline_recovery_raw_text",
                "nextbid_read_file",
            }
            or src.endswith(("raw_text", "read_file", "document0"))
        )
    )
    durable_ok = bool(
        row
        and int(row.get("status_code") or 200) < 400
        and (len(text) >= 80 or (row.get("content_hash") and row.get("body") is not None))
    )
    already = durable_ok or (len(text) >= 80) or ft_covers
    return {
        "nb_id": nb_id,
        "relative_path": relative_path,
        "http_path": http_path,
        "request_id": rid,
        "durable_response_present": row is not None,
        "durable_text_chars": len(text),
        "full_text_chars": len(ft_text),
        "full_text_source": ft.get("source"),
        "content_hash": (row or {}).get("content_hash"),
        "original_bytes_sha256": (row or {}).get("original_bytes_sha256"),
        "retrieved_at": (row or {}).get("retrieved_at"),
        "already_retained": already,
        "persistence_ok": bool(row and (row.get("content_hash") or row.get("body"))),
    }


def _document_entry_from_source(nb_id: str, d: dict[str, Any], *, acquired_at: str | None) -> dict[str, Any]:
    sha = d.get("sha256")
    local = local_pdf_bytes_by_sha(str(sha) if sha else None)
    download = str(d.get("download") or "")
    # Normalize API path (strip /v1 prefix used in provider notes)
    api_path = download
    if api_path.startswith("/v1/"):
        api_path = api_path[3:]
    return {
        "role": "attachment",
        "kind": d.get("kind") or "attachment",
        "original_filename": d.get("name") or d.get("filename"),
        "provider_document_id": sha,  # NextBid uses content sha256 as download id
        "content_sha256": sha,
        "declared_bytes": d.get("bytes"),
        "provider_stored": bool(d.get("stored")),
        "provider_availability": d.get("availability"),
        "provider_store": d.get("store"),
        "provider_store_key": d.get("store_key"),
        "provider_download_path": download or None,
        "provider_api_path": api_path or None,
        "source_url": d.get("source_url") or d.get("url"),
        "provider_downloaded_at": d.get("downloaded_at"),
        "provider_note": d.get("note"),
        "local_pdf_bytes_present": local["present"],
        "local_pdf_path": local["path"],
        "local_extracted_text_present": False,
        "local_extracted_text_chars": 0,
        "local_extracted_text_ref": None,
        "m3_retrieval_status": "local_bytes" if local["present"] else (
            "metadata_only_provider_stored" if d.get("stored") else "metadata_only_unstored"
        ),
        "retrieval_method_if_missing": {
            "endpoint": "download_document",
            "method": "GET",
            "path": api_path or None,
            "parameters": {"nb_id": nb_id, "document_sha256": sha},
            "expected_content_type": "application/pdf OR signed-url redirect",
            "returns": "UNVERIFIED — provider note says download link issued per request",
            "intermediate_metadata_required": False,
            "list_documents_already_retained": True,
            "max_credit_cost": "UNVERIFIED (likely 1 if metered; confirm before spend)",
            "proven_from": "retained list_documents.source_documents[].download",
        } if not local["present"] else None,
        "metadata_acquired_at": acquired_at,
        "version": None,
        "is_addendum": False,
        "missing": not local["present"],
    }


def _document_entry_from_file(
    nb_id: str, f: dict[str, Any], *, acquired_at: str | None
) -> dict[str, Any]:
    path = str(f.get("path") or "")
    rel = path
    # path may be absolute http path
    marker = f"/opportunities/{nb_id}/files/"
    if marker in path:
        rel = path.split(marker, 1)[1]
    retention = file_artifact_retention(nb_id, rel) if rel else {
        "already_retained": False,
        "durable_text_chars": 0,
        "http_path": None,
        "request_id": None,
        "content_hash": None,
        "original_bytes_sha256": None,
        "retrieved_at": None,
        "persistence_ok": False,
    }
    return {
        "role": "notice" if str(f.get("kind") or "").startswith("document0") else "nextbid_file",
        "kind": f.get("kind") or f.get("class") or "file",
        "original_filename": Path(rel).name if rel else None,
        "provider_document_id": rel or None,
        "content_sha256": retention.get("original_bytes_sha256"),
        "declared_bytes": f.get("bytes"),
        "provider_stored": True,  # listed in NextBid files index
        "provider_availability": "available",
        "provider_store": "nextbid-files",
        "provider_store_key": rel or None,
        "provider_download_path": None,
        "provider_api_path": retention.get("http_path") or (f"/opportunities/{nb_id}/files/{rel}" if rel else None),
        "source_url": None,
        "provider_downloaded_at": f.get("mtime"),
        "provider_note": "read with GET .../files/<path>",
        "local_pdf_bytes_present": False,
        "local_pdf_path": None,
        "local_extracted_text_present": bool(retention.get("already_retained")),
        "local_extracted_text_chars": int(retention.get("durable_text_chars") or 0)
        or (int((load_full_text(nb_id) or {}).get("full_text_chars") or 0) if retention.get("already_retained") else 0),
        "local_extracted_text_ref": {
            "paid_response_request_id": retention.get("request_id"),
            "content_hash": retention.get("content_hash"),
            "full_text_store": nb_id if retention.get("already_retained") else None,
        },
        "m3_retrieval_status": "local_text" if retention.get("already_retained") else "metadata_only_not_retrieved",
        "retrieval_method_if_missing": {
            "endpoint": "read_file",
            "method": "GET",
            "path": retention.get("http_path"),
            "parameters": {"nb_id": nb_id, "path": rel},
            "expected_content_type": "text/markdown" if rel.endswith(".md") else "application/json",
            "returns": "file body (text/markdown or JSON)",
            "intermediate_metadata_required": False,
            "max_credit_cost": 1,
            "proven_from": "retained list_files / nextbid_documents",
        } if not retention.get("already_retained") else None,
        "metadata_acquired_at": acquired_at,
        "content_acquired_at": retention.get("retrieved_at"),
        "version": None,
        "is_addendum": False,
        "missing": not retention.get("already_retained"),
        "duplicate_purchase_blocked": bool(retention.get("already_retained")),
    }


def build_solicitation_document_manifest(nb_id: str) -> dict[str, Any]:
    """Assemble canonical manifest for one opportunity from retained evidence only."""
    rows = _iter_nb_paid_rows(nb_id)
    pkg = load_recovery_package(nb_id) or {}
    inv_title = pkg.get("title")
    solicitation_number = pkg.get("solicitation_number")
    agency = pkg.get("agency")
    source_url = pkg.get("source_url")
    amendments: list[Any] = []
    source_documents: list[dict[str, Any]] = []
    nextbid_documents: list[dict[str, Any]] = []
    file_index: list[dict[str, Any]] = []
    list_docs_at = None
    list_files_at = None
    docs_meta: dict[str, Any] = {}

    for r in rows:
        ep = r.get("endpoint")
        body = r.get("body") if isinstance(r.get("body"), dict) else {}
        if ep == "get_opportunity":
            opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
            if isinstance(opp, dict):
                inv_title = inv_title or opp.get("title")
                solicitation_number = solicitation_number or opp.get("solicitation_number")
                agency = agency or opp.get("agency")
                source_url = source_url or opp.get("source_url")
                amendments = opp.get("amendments") or amendments
        elif ep == "list_documents":
            list_docs_at = r.get("retrieved_at")
            source_documents = body.get("source_documents") or source_documents
            nextbid_documents = body.get("nextbid_documents") or nextbid_documents
            for k in ("originals_stored", "originals_available", "source_documents_count", "downloads", "notes"):
                if body.get(k) not in (None, "", [], {}):
                    docs_meta[k] = body.get(k)
        elif ep == "list_files":
            list_files_at = r.get("retrieved_at")
            files = body.get("files") or []
            if isinstance(files, list):
                file_index = [f for f in files if isinstance(f, dict)]

    documents: list[dict[str, Any]] = []
    for d in source_documents:
        if isinstance(d, dict):
            documents.append(_document_entry_from_source(nb_id, d, acquired_at=list_docs_at))
    # Prefer list_files for document0; fall back to nextbid_documents
    file_rows = file_index or [x for x in nextbid_documents if isinstance(x, dict)]
    for f in file_rows:
        documents.append(_document_entry_from_file(nb_id, f, acquired_at=list_files_at or list_docs_at))

    attachments = [d for d in documents if d.get("role") == "attachment"]
    notices = [d for d in documents if d.get("role") == "notice"]
    local_text = sum(1 for d in documents if d.get("local_extracted_text_present"))
    local_pdf = sum(1 for d in documents if d.get("local_pdf_bytes_present"))
    missing = sum(1 for d in documents if d.get("missing"))

    return {
        "manifest_version": MANIFEST_VERSION,
        "updated_at": _utc(),
        "canonical_solicitation_id": solicitation_number or nb_id,
        "provider": "nextbid",
        "provider_opportunity_id": nb_id,
        "solicitation_number": solicitation_number,
        "title": inv_title,
        "agency": agency,
        "official_url": source_url,
        "addenda_amendments": amendments or [],
        "provider_document_meta": docs_meta,
        "documents": documents,
        "counts": {
            "total_documents": len(documents),
            "attachments": len(attachments),
            "notice_artifacts": len(notices),
            "local_text_bodies": local_text,
            "local_pdf_bytes": local_pdf,
            "missing_local_content": missing,
            "provider_stored_attachments": sum(1 for d in attachments if d.get("provider_stored")),
        },
        "distinction": {
            "nextbid_stored_true": "Provider object-store holds original; not proof of M3 local bytes",
            "m3_local_pdf_bytes": "evidence_vault / local path presence",
            "m3_local_text": "paid response store and/or solicitation_full_text",
        },
        "evidence_trail": {
            "paid_response_rows": len(rows),
            "list_documents_at": list_docs_at,
            "list_files_at": list_files_at,
            "endpoints_seen": sorted({str(r.get("endpoint")) for r in rows}),
        },
    }


def save_manifest(manifest: dict[str, Any], *, root: Path | None = None) -> Path:
    nb_id = str(manifest.get("provider_opportunity_id") or "")
    path = manifest_path(nb_id, root=root)
    _atomic_write_json(path, manifest)
    return path


def load_manifest(nb_id: str, *, root: Path | None = None) -> dict[str, Any] | None:
    path = manifest_path(nb_id, root=root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def sync_manifest_to_registry(manifest: dict[str, Any]) -> dict[str, Any]:
    """Attach manifest to permanent procurement registry record (file-backed)."""
    from m3.research.procurement_registry import upsert_record

    slim_docs = []
    for d in manifest.get("documents") or []:
        slim_docs.append(
            {
                "original_filename": d.get("original_filename"),
                "role": d.get("role"),
                "kind": d.get("kind"),
                "provider_document_id": d.get("provider_document_id"),
                "content_sha256": d.get("content_sha256"),
                "provider_stored": d.get("provider_stored"),
                "provider_api_path": d.get("provider_api_path"),
                "provider_store_key": d.get("provider_store_key"),
                "source_url": d.get("source_url"),
                "m3_retrieval_status": d.get("m3_retrieval_status"),
                "local_pdf_bytes_present": d.get("local_pdf_bytes_present"),
                "local_extracted_text_present": d.get("local_extracted_text_present"),
                "local_extracted_text_chars": d.get("local_extracted_text_chars"),
                "missing": d.get("missing"),
                "duplicate_purchase_blocked": d.get("duplicate_purchase_blocked"),
            }
        )
    return upsert_record(
        {
            "provider": "nextbid",
            "provider_id": manifest.get("provider_opportunity_id"),
            "nb_id": manifest.get("provider_opportunity_id"),
            "solicitation_number": manifest.get("solicitation_number"),
            "title": manifest.get("title"),
            "agency": manifest.get("agency"),
            "official_url": manifest.get("official_url"),
            "document_manifest": slim_docs,
            "package_status": (
                "COMPLETE"
                if int((manifest.get("counts") or {}).get("missing_local_content") or 0) == 0
                else "PARTIAL"
            ),
            "build_origins": ["solicitation_document_manifest"],
        },
        actor="solicitation_document_manifest",
    )


def ensure_notice_in_evidence_vault(nb_id: str, relative_path: str) -> dict[str, Any] | None:
    """Copy already-paid notice text into evidence vault (no API)."""
    status = file_artifact_retention(nb_id, relative_path)
    if not status.get("durable_response_present"):
        return None
    row = load_by_request_id(status["request_id"]) or {}
    text = nextbid_file_text(row.get("body"))
    if not text:
        return None
    from m3.research import evidence_vault

    return evidence_vault.put(
        "nextbid",
        nb_id,
        "document0_notice",
        text.encode("utf-8"),
        url=status.get("http_path"),
        status=200,
        content_type="text/markdown",
        retrieved_at=row.get("retrieved_at"),
        meta={"path": relative_path, "request_id": status["request_id"], "offline_copy": True},
    )


def refresh_enrichment_retrieval_flags(nb_id: str, manifest: dict[str, Any]) -> None:
    """Update enrichment.jsonl recovered_file_texts flags so planners see retained files."""
    import m3.research.nextbid_offline_recovery as recovery

    idx = recovery.load_enrichment_index(force=True)
    row = idx.get(nb_id)
    if not row:
        return
    recovered = []
    for d in manifest.get("documents") or []:
        if d.get("role") != "notice":
            continue
        if not d.get("local_extracted_text_present"):
            continue
        path = d.get("provider_store_key") or d.get("original_filename")
        recovered.append(
            {
                "path": (
                    f"/opportunities/{nb_id}/files/{path}"
                    if path and not str(path).startswith("/")
                    else path
                ),
                "chars": d.get("local_extracted_text_chars"),
                "sha256": d.get("content_sha256"),
                "previously_ignored_raw_text": False,
            }
        )
    row = dict(row)
    row["recovered_file_texts"] = recovered
    row["local_original_text_bodies"] = int((manifest.get("counts") or {}).get("local_text_bodies") or 0)
    row["local_original_pdf_bytes_available"] = int((manifest.get("counts") or {}).get("local_pdf_bytes") or 0)
    row["document_manifest_synced_at"] = _utc()
    eps = set(row.get("endpoints_present") or [])
    eps.add("read_file")
    row["endpoints_present"] = sorted(eps)
    idx[nb_id] = row
    recovery._write_jsonl(recovery.enrichment_path(), list(idx.values()))
    recovery._ENRICHMENT_CACHE = None
