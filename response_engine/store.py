"""Persistent ResponseProject store — idempotent, restart-safe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.quote_economics import load_json, save_json

ROOT = Path(__file__).resolve().parents[1]
STORE_DIR = ROOT / "data" / "response_projects"
INDEX_PATH = STORE_DIR / "index.json"


def _utc() -> str:
    return now_utc().isoformat()


def ensure_store() -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        save_json(INDEX_PATH, {"kind": "ResponseProjectIndex", "by_id": {}, "by_opportunity": {}, "updated_at": _utc()})
    return STORE_DIR


def _load_index() -> dict[str, Any]:
    ensure_store()
    try:
        return load_json(INDEX_PATH)
    except Exception:
        return {"kind": "ResponseProjectIndex", "by_id": {}, "by_opportunity": {}, "updated_at": None}


def _save_index(index: dict[str, Any]) -> None:
    index["updated_at"] = _utc()
    save_json(INDEX_PATH, index)


def project_path(response_project_id: str) -> Path:
    return STORE_DIR / f"{response_project_id}.json"


def save_project(project: dict[str, Any]) -> dict[str, Any]:
    ensure_store()
    rid = project["response_project_id"]
    project["updated_at"] = _utc()
    save_json(project_path(rid), project)
    index = _load_index()
    index.setdefault("by_id", {})[rid] = {
        "response_project_id": rid,
        "canonical_opportunity_id": project.get("canonical_opportunity_id"),
        "buyer": project.get("buyer"),
        "solicitation_number": project.get("solicitation_number"),
        "response_status": project.get("response_status"),
        "updated_at": project["updated_at"],
    }
    cid = project.get("canonical_opportunity_id")
    if cid:
        index.setdefault("by_opportunity", {})[str(cid)] = rid
    _save_index(index)
    return project


def load_project(response_project_id: str) -> dict[str, Any] | None:
    path = project_path(response_project_id)
    if not path.exists():
        return None
    return load_json(path)


def find_by_opportunity(canonical_opportunity_id: str) -> dict[str, Any] | None:
    index = _load_index()
    rid = (index.get("by_opportunity") or {}).get(str(canonical_opportunity_id))
    if not rid:
        return None
    return load_project(rid)


def list_projects(*, limit: int = 100) -> list[dict[str, Any]]:
    index = _load_index()
    rows = list((index.get("by_id") or {}).values())
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows[:limit]
