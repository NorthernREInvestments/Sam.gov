"""Authorized operator document ingestion — hash, version, classify, store.

Does NOT bypass portal auth. Accepts files the operator obtained legitimately.
"""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from document_ingestion_constants import (
    ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
    ACQ_AUTHORIZED_PORTAL_EXPORT,
    ACQ_MANUAL_UPLOAD,
    ACQ_PUBLIC_FETCH,
    ACQ_UNKNOWN,
    ACQUISITION_METHODS,
    SUPPORTED_INGEST_EXTENSIONS,
)
from solicitation_package_retrieval import classify_document, content_sha256, file_type_from_name
from table_extractors import extract_from_bytes, safe_inspect_zip

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
INBOX_ROOT = ARTIFACTS / "document_inbox"
REGISTRY_ROOT = ARTIFACTS / "document_registry"
CHANGE_HISTORY_PATH = ARTIFACTS / "document_change_history.json"


def _utc() -> str:
    return now_utc().isoformat()


def normalize_acquisition_method(raw: str | None) -> str:
    if not raw:
        return ACQ_UNKNOWN
    key = re.sub(r"[\s\-]+", "_", str(raw).strip().upper())
    aliases = {
        "PUBLIC": ACQ_PUBLIC_FETCH,
        "PUBLIC_FETCH": ACQ_PUBLIC_FETCH,
        "AUTHORIZED_OPERATOR_DOWNLOAD": ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
        "OPERATOR_DOWNLOAD": ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
        "MANUAL": ACQ_MANUAL_UPLOAD,
        "MANUAL_UPLOAD": ACQ_MANUAL_UPLOAD,
        "UPLOAD": ACQ_MANUAL_UPLOAD,
        "AUTHORIZED_PORTAL_EXPORT": ACQ_AUTHORIZED_PORTAL_EXPORT,
        "PORTAL_EXPORT": ACQ_AUTHORIZED_PORTAL_EXPORT,
        "EXPORT": ACQ_AUTHORIZED_PORTAL_EXPORT,
    }
    method = aliases.get(key, key)
    if method not in ACQUISITION_METHODS:
        return ACQ_UNKNOWN
    return method


def registry_path(solicitation_number: str) -> Path:
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", solicitation_number)
    return REGISTRY_ROOT / f"{safe}.json"


def load_registry(solicitation_number: str) -> dict[str, Any]:
    path = registry_path(solicitation_number)
    if not path.exists():
        return {
            "solicitation_number": solicitation_number,
            "documents": [],
            "updated_at": None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(registry: dict[str, Any]) -> Path:
    sol = registry["solicitation_number"]
    registry["updated_at"] = _utc()
    path = registry_path(sol)
    path.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")
    return path


def append_change_history(entry: dict[str, Any]) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if CHANGE_HISTORY_PATH.exists():
        try:
            history = json.loads(CHANGE_HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    history.append(entry)
    CHANGE_HISTORY_PATH.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


def _extract_text_and_tables(
    data: bytes,
    *,
    filename: str,
) -> dict[str, Any]:
    ext = Path(filename).suffix.lower()
    text = ""
    tables: dict[str, Any] | None = None
    members: list[dict[str, Any]] = []

    if ext == ".zip" or data[:2] == b"PK" and ext == ".zip":
        z = safe_inspect_zip(data)
        if not z.get("ok"):
            return {"text": "", "tables": None, "zip": z, "error": z.get("error")}
        for mem in z.get("extracted") or []:
            members.append(
                {
                    "name": mem.get("name"),
                    "sha256": mem.get("sha256"),
                    "extension": mem.get("extension"),
                    "size": mem.get("size"),
                }
            )
            nested = extract_from_bytes(mem["bytes"], filename=mem.get("safe_name") or mem.get("name"))
            if nested.get("raw_text"):
                text += "\n" + nested["raw_text"]
            if nested.get("line_items") and not tables:
                tables = nested
            # PDF text via extract_from_bytes
            if (mem.get("extension") or "").lower() == ".pdf" or (mem.get("bytes") or b"").startswith(b"%PDF"):
                from pdf_text import extract_pdf_text

                text += "\n" + (extract_pdf_text(mem["bytes"]) or "")
        return {"text": text.strip(), "tables": tables, "zip": z, "members": members, "error": None}

    if ext == ".pdf" or data.startswith(b"%PDF"):
        from pdf_text import extract_pdf_text

        text = extract_pdf_text(data) or ""
        tables = extract_from_bytes(data, filename=filename)
        return {"text": text, "tables": tables, "zip": None, "members": [], "error": None}

    if ext == ".txt":
        text = data.decode("utf-8", errors="replace")
        return {"text": text, "tables": None, "zip": None, "members": [], "error": None}

    if ext in {".docx", ".xlsx", ".xls", ".csv", ".doc"}:
        tables = extract_from_bytes(data, filename=filename)
        text = (tables or {}).get("raw_text") or ""
        if not text and tables and tables.get("line_items"):
            text = "\n".join(
                str(li.get("description") or "") for li in tables["line_items"] if li.get("description")
            )
        return {"text": text, "tables": tables, "zip": None, "members": [], "error": tables.get("error") if tables else None}

    return {"text": "", "tables": None, "zip": None, "members": [], "error": "unsupported_or_empty"}


def ingest_solicitation_document(
    *,
    solicitation_number: str,
    file_path: str | Path,
    source_system: str = "sciquest",
    acquisition_method: str = ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
    document_title: str | None = None,
    document_type: str | None = None,
    opportunity_id: str | None = None,
    deal_id: str | None = None,
    source_url: str | None = None,
    amendment_sequence: int | None = None,
    version_label: str | None = None,
    operator_notes: str | None = None,
) -> dict[str, Any]:
    """
    Ingest one operator-provided file into the solicitation document registry.
    Identical hashes short-circuit as duplicates (no second evidence blob).
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "file_not_found", "path": str(file_path)}

    ext = path.suffix.lower()
    if ext not in SUPPORTED_INGEST_EXTENSIONS:
        return {"ok": False, "error": "unsupported_extension", "extension": ext}

    data = path.read_bytes()
    file_hash = content_sha256(data)
    acq = normalize_acquisition_method(acquisition_method)
    title = document_title or path.name
    doc_class = document_type or classify_document(title=title, filename=path.name, url=source_url)

    registry = load_registry(solicitation_number)
    existing = [d for d in registry.get("documents") or [] if d.get("hash") == file_hash]
    if existing:
        return {
            "ok": True,
            "duplicate": True,
            "ingested": False,
            "document": existing[0],
            "message": "identical_hash_already_registered",
            "supplier_outreach": 0,
            "lender_outreach": 0,
            "bid_submissions": 0,
        }

    # Version group by title/class — preserve prior versions, mark superseded when amendment
    same_title = [
        d
        for d in registry.get("documents") or []
        if (d.get("document_title") or "").lower() == title.lower()
        or (
            d.get("document_class") == doc_class == "AMENDMENT"
            and (d.get("original_filename") or "").lower() == path.name.lower()
        )
    ]
    version_index = len(same_title) + 1
    if amendment_sequence is None and doc_class == "AMENDMENT":
        amendment_sequence = version_index

    dest_dir = INBOX_ROOT / re.sub(r"[^\w.\-]+", "_", solicitation_number)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{file_hash[:16]}__{path.name}"
    dest = dest_dir / stored_name
    shutil.copy2(path, dest)

    extracted = _extract_text_and_tables(data, filename=path.name)
    # Persist extracted text beside binary for reprocess without re-reading large binaries always
    text_path = dest.with_suffix(dest.suffix + ".txt")
    if extracted.get("text"):
        text_path.write_text(extracted["text"], encoding="utf-8")

    record = {
        "document_id": f"{solicitation_number}:{file_hash[:16]}",
        "solicitation_number": solicitation_number,
        "source_system": source_system,
        "opportunity_id": opportunity_id or solicitation_number,
        "deal_id": deal_id,
        "document_title": title,
        "document_type": doc_class,
        "document_class": doc_class,
        "original_filename": path.name,
        "acquisition_method": acq,
        "acquisition_timestamp": _utc(),
        "operator_source_url": source_url,
        "hash": file_hash,
        "content_length": len(data),
        "file_type": file_type_from_name(path.name),
        "local_path": str(dest),
        "text_path": str(text_path) if extracted.get("text") else None,
        "text_length": len(extracted.get("text") or ""),
        "amendment_sequence": amendment_sequence,
        "version_label": version_label or f"v{version_index}",
        "version_index": version_index,
        "superseded": False,
        "appears_authoritative": True,  # operator-authorized docs are not weaker
        "access_status": "OPERATOR_PROVIDED",
        "provenance": f"acquisition:{acq}",
        "operator_notes": operator_notes,
        "extraction_status": "TEXT_EXTRACTED" if extracted.get("text") else "STORED_NO_TEXT",
        "extraction_error": extracted.get("error"),
        "zip_members": extracted.get("members") or [],
        "table_extraction": {
            "ok": bool((extracted.get("tables") or {}).get("ok")),
            "line_item_count": len((extracted.get("tables") or {}).get("line_items") or []),
            "method": (extracted.get("tables") or {}).get("extraction_method"),
        }
        if extracted.get("tables")
        else None,
    }

    # Mark older same-title / lower amendment sequence as superseded when new amendment arrives
    if doc_class == "AMENDMENT":
        for d in registry.get("documents") or []:
            if d.get("document_class") in {"SOLICITATION", "AMENDMENT"} and not d.get("superseded"):
                prior_seq = d.get("amendment_sequence") or 0
                if (amendment_sequence or 0) > prior_seq:
                    d["superseded"] = True
                    d["superseded_by"] = record["document_id"]
                    d["authority_note"] = "later_amendment_may_override"

    # Newer version of same specification title supersedes older copies (preserve records)
    for d in same_title:
        if not d.get("superseded"):
            d["superseded"] = True
            d["superseded_by"] = record["document_id"]

    registry.setdefault("documents", []).append(record)
    save_registry(registry)

    append_change_history(
        {
            "event": "DOCUMENT_INGESTED",
            "solicitation_number": solicitation_number,
            "document_id": record["document_id"],
            "document_title": title,
            "document_class": doc_class,
            "acquisition_method": acq,
            "duplicate": False,
            "timestamp": _utc(),
        }
    )

    return {
        "ok": True,
        "duplicate": False,
        "ingested": True,
        "document": record,
        "registry_path": str(registry_path(solicitation_number)),
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }


def ingest_many(
    *,
    solicitation_number: str,
    file_paths: list[str | Path],
    **kwargs: Any,
) -> dict[str, Any]:
    results = []
    for fp in file_paths:
        results.append(ingest_solicitation_document(solicitation_number=solicitation_number, file_path=fp, **kwargs))
    return {
        "ok": all(r.get("ok") for r in results),
        "count": len(results),
        "ingested_count": sum(1 for r in results if r.get("ingested")),
        "duplicate_count": sum(1 for r in results if r.get("duplicate")),
        "results": results,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }


def list_operator_document_requests(packet: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build operator request rows from a procurement packet's auth-blocked docs."""
    packet = packet or {}
    rows = []
    for d in packet.get("documents") or []:
        if d.get("access_status") in {"LOGIN_REQUIRED", "AUTH_REQUIRED", "PUBLIC_LISTED_NOT_FETCHED"}:
            rows.append(
                {
                    "solicitation": packet.get("solicitation_number"),
                    "document_title": d.get("document_title"),
                    "document_class": d.get("document_class"),
                    "access_status": d.get("access_status"),
                    "request": "OPERATOR_DOCUMENT_REQUIRED",
                    "acquisition_hint": ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
                    "instructions": (
                        "Download via authorized Jaggaer/SciQuest supplier account, "
                        "then run scripts/ingest_solicitation_document.py"
                    ),
                }
            )
    return rows


def load_ingested_texts(solicitation_number: str) -> list[dict[str, Any]]:
    """Return current (non-superseded) ingested docs with text for reprocessing."""
    registry = load_registry(solicitation_number)
    out = []
    for d in registry.get("documents") or []:
        if d.get("superseded"):
            continue
        text = ""
        tp = d.get("text_path")
        if tp and Path(tp).exists():
            text = Path(tp).read_text(encoding="utf-8", errors="replace")
        elif d.get("local_path") and Path(d["local_path"]).exists():
            data = Path(d["local_path"]).read_bytes()
            extracted = _extract_text_and_tables(data, filename=d.get("original_filename") or "doc")
            text = extracted.get("text") or ""
        out.append({**d, "text": text})
    return out
