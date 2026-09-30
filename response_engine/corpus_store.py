"""R1.3 real solicitation corpus store — hash-deduped buyer packages (0 SAM API)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from application_clock import now_utc
from phase_l.quote_economics import load_json, save_json

ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "data" / "response_corpus" / "real"
MANIFEST_PATH = CORPUS_ROOT / "real_corpus_manifest.json"
BUILD = "20260929-m3-r13-real-corpus-final-validation"
PARSER_VERSION = "r11-20260929-v1"
MAX_BYTES = 40 * 1024 * 1024


def _utc() -> str:
    return now_utc().isoformat()


def ensure_corpus() -> Path:
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_PATH.exists():
        save_json(
            MANIFEST_PATH,
            {
                "kind": "RealCorpusManifest",
                "build": BUILD,
                "projects": {},
                "updated_at": _utc(),
            },
        )
    return CORPUS_ROOT


def load_manifest() -> dict[str, Any]:
    ensure_corpus()
    try:
        return load_json(MANIFEST_PATH)
    except Exception:
        return {"kind": "RealCorpusManifest", "build": BUILD, "projects": {}, "updated_at": None}


def save_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _utc()
    manifest["build"] = BUILD
    save_json(MANIFEST_PATH, manifest)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]+", "_", name)[:160] or "document.bin"


def fetch_public_url(url: str, *, timeout: int = 45) -> dict[str, Any]:
    """Bounded public fetch. Never hits api.sam.gov."""
    if "api.sam.gov" in url.lower():
        return {"ok": False, "status": "SAM_API_BLOCKED", "url": url, "error": "SAM API blocked by policy"}
    try:
        # Browser-like UA: many agency CDNs reject custom library UAs with 403.
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
            },
        )
        with urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(MAX_BYTES + 1)
            final_url = resp.geturl()
        if len(data) > MAX_BYTES:
            return {"ok": False, "status": "TOO_LARGE", "url": url, "error": f">{MAX_BYTES}"}
        if not data:
            return {"ok": False, "status": "EMPTY", "url": url}
        # login/anti-bot HTML when expecting a file
        head = data[:2500].lower()
        name_guess = Path(urlparse(url).path).name or "download.bin"
        if name_guess.lower().endswith(".pdf") and not data.startswith(b"%PDF"):
            if b"<html" in head or b"login" in head or b"sign in" in head:
                return {"ok": False, "status": "AUTH_REQUIRED" if b"login" in head or b"sign in" in head else "CONTENT_TYPE_MISMATCH", "url": url}
            return {"ok": False, "status": "CONTENT_TYPE_MISMATCH", "url": url}
        if b"captcha" in head or b"access denied" in head:
            return {"ok": False, "status": "ANTI_BOT_BLOCKED", "url": url}
        return {
            "ok": True,
            "status": "FETCHED",
            "url": url,
            "final_url": final_url,
            "content_type": ctype,
            "data": data,
            "filename": sanitize_filename(name_guess),
            "sha256": sha256_bytes(data),
            "bytes": len(data),
            "retrieved_at": _utc(),
        }
    except Exception as exc:
        msg = str(exc).lower()
        status = "AUTH_REQUIRED" if ("401" in msg or "403" in msg) else "FETCH_FAILED"
        return {"ok": False, "status": status, "url": url, "error": str(exc)[:300]}


def project_dir(corpus_project_id: str) -> Path:
    return CORPUS_ROOT / corpus_project_id


def upsert_project(
    *,
    corpus_project_id: str,
    buyer: str,
    solicitation_number: str | None = None,
    title: str | None = None,
    jurisdiction: str | None = None,
    agency: str | None = None,
    authoritative_source: str | None = None,
    discovery_source: str | None = None,
    submission_system: str | None = None,
    tags: list[str] | None = None,
    status: str = "CLOSED_OR_HISTORICAL",
    notes: str | None = None,
) -> dict[str, Any]:
    ensure_corpus()
    manifest = load_manifest()
    projects = manifest.setdefault("projects", {})
    existing = projects.get(corpus_project_id) or {
        "corpus_project_id": corpus_project_id,
        "documents": [],
        "hashes": [],
        "tags": [],
        "created_at": _utc(),
    }
    existing.update(
        {
            "buyer": buyer,
            "agency": agency or existing.get("agency"),
            "solicitation_number": solicitation_number or existing.get("solicitation_number"),
            "title": title or existing.get("title"),
            "jurisdiction": jurisdiction or existing.get("jurisdiction"),
            "authoritative_source": authoritative_source or existing.get("authoritative_source"),
            "discovery_source": discovery_source or existing.get("discovery_source"),
            "submission_system": submission_system or existing.get("submission_system"),
            "solicitation_status": status,
            "notes": notes or existing.get("notes"),
            "updated_at": _utc(),
        }
    )
    for t in tags or []:
        if t not in existing["tags"]:
            existing["tags"].append(t)
    projects[corpus_project_id] = existing
    save_manifest(manifest)
    project_dir(corpus_project_id).mkdir(parents=True, exist_ok=True)
    return existing


def add_bytes_to_project(
    corpus_project_id: str,
    *,
    data: bytes,
    filename: str,
    source_url: str | None = None,
    role: str | None = None,
    acquisition: str = "PUBLIC_FETCH",
) -> dict[str, Any]:
    manifest = load_manifest()
    project = (manifest.get("projects") or {}).get(corpus_project_id)
    if not project:
        raise KeyError(corpus_project_id)
    h = sha256_bytes(data)
    # dedupe by hash within project
    for d in project.get("documents") or []:
        if d.get("sha256") == h:
            return {"ok": True, "duplicate": True, "document": d}
    safe = sanitize_filename(filename)
    dest = project_dir(corpus_project_id) / f"{h[:16]}_{safe}"
    if not dest.exists():
        dest.write_bytes(data)
    doc = {
        "filename": filename,
        "stored_as": dest.name,
        "path": str(dest),
        "sha256": h,
        "bytes": len(data),
        "source_url": source_url,
        "role": role,
        "acquisition": acquisition,
        "retrieved_at": _utc(),
    }
    project.setdefault("documents", []).append(doc)
    project.setdefault("hashes", []).append(h)
    project["updated_at"] = _utc()
    manifest["projects"][corpus_project_id] = project
    save_manifest(manifest)
    # per-project sidecar
    save_json(project_dir(corpus_project_id) / "project.json", project)
    return {"ok": True, "duplicate": False, "document": doc}


def add_local_file(
    corpus_project_id: str,
    path: Path,
    *,
    source_url: str | None = None,
    role: str | None = None,
    acquisition: str = "LOCAL_ARTIFACT",
) -> dict[str, Any]:
    data = path.read_bytes()
    return add_bytes_to_project(
        corpus_project_id,
        data=data,
        filename=path.name,
        source_url=source_url or f"file://{path}",
        role=role,
        acquisition=acquisition,
    )


def add_url_to_project(
    corpus_project_id: str,
    url: str,
    *,
    filename: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    fetched = fetch_public_url(url)
    if not fetched.get("ok"):
        return fetched
    return {
        **add_bytes_to_project(
            corpus_project_id,
            data=fetched["data"],
            filename=filename or fetched["filename"],
            source_url=fetched.get("final_url") or url,
            role=role,
            acquisition="PUBLIC_FETCH",
        ),
        "fetch_status": fetched["status"],
        "sha256": fetched["sha256"],
    }


def list_projects() -> list[dict[str, Any]]:
    return list((load_manifest().get("projects") or {}).values())
