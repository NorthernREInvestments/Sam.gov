"""Shared master solicitation store (DLA / agency masters) — versioned, immutable."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.quote_economics import load_json, save_json
from response_engine.models import new_id
from response_engine.parsers import parse_file_bytes, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
MASTER_DIR = ROOT / "data" / "shared_master_documents"
INDEX_PATH = MASTER_DIR / "index.json"
BINARY_DIR = MASTER_DIR / "binaries"

MASTER_VERSION_REVIEW_REQUIRED = "MASTER_VERSION_REVIEW_REQUIRED"


def _utc() -> str:
    return now_utc().isoformat()


def ensure_master_store() -> Path:
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    BINARY_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        save_json(INDEX_PATH, {"kind": "SharedMasterDocumentIndex", "by_id": {}, "by_authority_title": {}, "updated_at": _utc()})
    return MASTER_DIR


def _load_index() -> dict[str, Any]:
    ensure_master_store()
    try:
        return load_json(INDEX_PATH)
    except Exception:
        return {"kind": "SharedMasterDocumentIndex", "by_id": {}, "by_authority_title": {}, "updated_at": None}


def _save_index(index: dict[str, Any]) -> None:
    index["updated_at"] = _utc()
    save_json(INDEX_PATH, index)


def register_master_document(
    *,
    authority: str,
    title: str,
    version: str,
    data: bytes,
    filename: str,
    source: str | None = None,
    effective_date: str | None = None,
    parsed_text: str | None = None,
) -> dict[str, Any]:
    """Store master once by hash+version. Never overwrite prior version bytes/metadata."""
    ensure_master_store()
    file_hash = sha256_bytes(data)
    index = _load_index()
    # Same authority+title+version+hash → return existing
    key = f"{authority}|{title}|{version}".lower()
    existing_ids = (index.get("by_authority_title") or {}).get(key) or []
    for mid in existing_ids:
        rec = (index.get("by_id") or {}).get(mid)
        if rec and rec.get("file_hash") == file_hash and rec.get("version") == version:
            return load_master(mid) or rec

    mid = new_id("MASTER")
    safe = re_sub_filename(filename)
    dest = BINARY_DIR / f"{mid}_{file_hash[:16]}_{safe}"
    if not dest.exists():
        dest.write_bytes(data)

    if parsed_text is None:
        parsed = parse_file_bytes(data, filename=filename)
        parsed_text = parsed.get("text") or ""
        parse_meta = {"method": parsed.get("method"), "confidence": parsed.get("confidence"), "parser_version": parsed.get("parser_version")}
    else:
        parse_meta = {"method": "provided", "confidence": "HIGH"}

    rec = {
        "kind": "SharedMasterDocument",
        "master_id": mid,
        "authority": authority,
        "title": title,
        "version": version,
        "effective_date": effective_date,
        "file_hash": file_hash,
        "content_hash": hashlib.sha256((parsed_text or "").encode("utf-8")).hexdigest() if parsed_text else file_hash,
        "source": source,
        "original_filename": filename,
        "binary_path": str(dest),
        "parsed_text_excerpt": (parsed_text or "")[:2000],
        "parse": parse_meta,
        "created_at": _utc(),
        # immutable: no updated overwrite of version identity
    }
    # persist full parsed text beside binary
    text_path = BINARY_DIR / f"{mid}.txt"
    text_path.write_text(parsed_text or "", encoding="utf-8")
    rec["parsed_text_path"] = str(text_path)

    index.setdefault("by_id", {})[mid] = {k: v for k, v in rec.items() if k != "parsed_text_excerpt"}
    index.setdefault("by_authority_title", {}).setdefault(key, []).append(mid)
    # Never remove prior versions from list
    _save_index(index)
    save_json(MASTER_DIR / f"{mid}.json", rec)
    return rec


def re_sub_filename(name: str) -> str:
    import re

    return re.sub(r"[^\w.\-]+", "_", name)[:160]


def load_master(master_id: str) -> dict[str, Any] | None:
    path = MASTER_DIR / f"{master_id}.json"
    if not path.exists():
        return None
    return load_json(path)


def list_masters(*, authority: str | None = None, title: str | None = None) -> list[dict[str, Any]]:
    index = _load_index()
    out = []
    for mid, rec in (index.get("by_id") or {}).items():
        if authority and (rec.get("authority") or "").lower() != authority.lower():
            continue
        if title and title.lower() not in (rec.get("title") or "").lower():
            continue
        out.append(rec)
    return out


def attach_master_to_project(
    project: dict[str, Any],
    master_id: str,
    *,
    applicability_confirmed: bool = False,
) -> dict[str, Any]:
    """Reference shared master from project graph — do not duplicate bytes into project store."""
    master = load_master(master_id)
    if not master:
        return {"ok": False, "error": "master_not_found"}
    project.setdefault("master_references", [])
    if any(m.get("master_id") == master_id for m in project["master_references"]):
        return {"ok": True, "duplicate": True, "master_id": master_id}
    ref = {
        "master_id": master_id,
        "authority": master.get("authority"),
        "title": master.get("title"),
        "version": master.get("version"),
        "file_hash": master.get("file_hash"),
        "applicability_confirmed": applicability_confirmed,
        "status": "ATTACHED" if applicability_confirmed else MASTER_VERSION_REVIEW_REQUIRED,
        "attached_at": _utc(),
    }
    project["master_references"].append(ref)
    project.setdefault("document_graph", {}).setdefault("edges", []).append(
        {"from": master_id, "to": project["response_project_id"], "relation": "MASTER_FOR", "version": master.get("version")}
    )
    if not applicability_confirmed:
        project.setdefault("hard_blocks", [])
        # soft review via clarifications preferred; status flag only
        project.setdefault("clarifications", [])
    return {"ok": True, "reference": ref}
