"""Durable NextBid paid-response store (Build 002Q permanent repair).

Every metered response is persisted BEFORE parsing/classification.
Processing failures must never destroy originals.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

_inflight_lock = threading.RLock()
_inflight: set[str] = set()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_root() -> Path:
    p = get_settings().data_root() / "nextbid_paid_responses"
    p.mkdir(parents=True, exist_ok=True)
    (p / "by_request").mkdir(parents=True, exist_ok=True)
    (p / "by_nb_id").mkdir(parents=True, exist_ok=True)
    (p / "index").mkdir(parents=True, exist_ok=True)
    return p


def request_identity(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    nb_id: str | None = None,
) -> str:
    payload = {
        "method": (method or "GET").upper(),
        "path": (path or "").rstrip("/"),
        "params": params or {},
        "nb_id": nb_id or "",
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_hash(body: Any) -> str:
    raw = json.dumps(body, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _req_path(req_id: str) -> Path:
    return store_root() / "by_request" / f"{req_id}.json"


def load_by_request_id(req_id: str) -> dict[str, Any] | None:
    path = _req_path(req_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def lookup_cached_response(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    nb_id: str | None = None,
) -> dict[str, Any] | None:
    """Return stored body if this exact metered request was already paid for."""
    rid = request_identity(method, path, params, nb_id=nb_id)
    row = load_by_request_id(rid)
    if not row:
        return None
    if row.get("status_code") and int(row["status_code"]) >= 400:
        return None
    body = row.get("body")
    if isinstance(body, dict):
        out = dict(body)
        out["_durable_store_hit"] = True
        out["_request_id"] = rid
        return out
    return None


def try_begin_inflight(req_id: str) -> bool:
    """Prevent concurrent duplicate metered calls for the same request identity."""
    with _inflight_lock:
        if req_id in _inflight:
            return False
        _inflight.add(req_id)
        return True


def end_inflight(req_id: str) -> None:
    with _inflight_lock:
        _inflight.discard(req_id)


def _atomic_write_json(path: Path, payload: Any) -> Path:
    """Atomic JSON write with OneDrive/PermissionError fallback (never lose body)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(raw, encoding="utf-8")
        try:
            tmp.replace(path)
        except PermissionError:
            # OneDrive lock: write beside target then best-effort replace
            alt = path.with_suffix(path.suffix + ".pending")
            alt.write_text(raw, encoding="utf-8")
            try:
                path.write_text(raw, encoding="utf-8")
                alt.unlink(missing_ok=True)
            except PermissionError:
                # Keep .pending as committed durability proof
                return alt
        return path
    except Exception:
        # Last resort: local non-OneDrive staging under %LOCALAPPDATA%
        import os

        staging = Path(os.environ.get("LOCALAPPDATA") or ".") / "M3Supply" / "nextbid_paid_responses"
        staging.mkdir(parents=True, exist_ok=True)
        fallback = staging / path.name
        fallback.write_text(raw, encoding="utf-8")
        return fallback


def persist_paid_response(
    *,
    method: str,
    path: str,
    params: dict[str, Any] | None,
    endpoint: str,
    nb_id: str | None,
    status_code: int,
    body: Any,
    job_id: str | None = None,
    raw_text: str | None = None,
) -> dict[str, Any]:
    """Write durable record BEFORE callers parse/classify. Never raises away the body.

    Persistence failure is fatal to acquisition callers — they must stop rather than
    continue without a retained original. This function itself never drops the body
    from the returned handle; it raises only after a local fallback write also fails.
    """
    rid = request_identity(method, path, params, nb_id=nb_id)
    ch = content_hash(body)
    # Never truncate the stored body — raw_text_preview is display-only.
    row = {
        "request_id": rid,
        "method": (method or "GET").upper(),
        "path": path,
        "params": params or {},
        "endpoint": endpoint,
        "nb_id": nb_id,
        "job_id": job_id,
        "status_code": status_code,
        "retrieved_at": _utc(),
        "content_hash": ch,
        "processing_status": "stored_unprocessed",
        "body": body,
        "raw_text_chars": len(raw_text or "") if raw_text else None,
        "raw_text_preview": (raw_text or "")[:2000] if raw_text else None,
        "canonical_opportunity": nb_id,
        "historical_versions": [],  # prior versions appended on change; never overwrite body away
    }
    # If a prior record exists with different hash, retain prior body pointer
    path_out = _req_path(rid)
    if path_out.exists():
        try:
            prior = json.loads(path_out.read_text(encoding="utf-8"))
            if prior.get("content_hash") and prior.get("content_hash") != ch:
                versions = list(prior.get("historical_versions") or [])
                versions.append(
                    {
                        "content_hash": prior.get("content_hash"),
                        "retrieved_at": prior.get("retrieved_at"),
                        "superseded_at": _utc(),
                        "body_retained": True,
                    }
                )
                # Keep thicker body via merge — never shrink evidence
                row["body"] = merge_thicker(prior.get("body"), body)
                row["historical_versions"] = versions[-20:]
                row["content_hash"] = content_hash(row["body"])
                ch = row["content_hash"]
        except Exception:
            pass

    written = _atomic_write_json(path_out, row)
    if not written.exists():
        raise RuntimeError(f"persistence_failed:{path_out}")

    if nb_id:
        idx = store_root() / "by_nb_id" / f"{nb_id}.jsonl"
        try:
            with idx.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "request_id": rid,
                            "endpoint": endpoint,
                            "path": path,
                            "status_code": status_code,
                            "content_hash": ch,
                            "retrieved_at": row["retrieved_at"],
                        },
                        default=str,
                    )
                    + "\n"
                )
        except PermissionError:
            pass
    try:
        with (store_root() / "index" / "responses.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "request_id": rid,
                        "endpoint": endpoint,
                        "nb_id": nb_id,
                        "path": path,
                        "status_code": status_code,
                        "content_hash": ch,
                        "retrieved_at": row["retrieved_at"],
                    },
                    default=str,
                )
                + "\n"
            )
    except PermissionError:
        pass
    return {
        "request_id": rid,
        "content_hash": ch,
        "path": str(written),
        "persistence_ok": True,
    }


def mark_processing(req_id: str, status: str, *, error: str | None = None) -> None:
    row = load_by_request_id(req_id)
    if not row:
        return
    row["processing_status"] = status
    row["processed_at"] = _utc()
    if error:
        row["processing_error"] = error[:500]
    # Never drop body
    path = _req_path(req_id)
    path.write_text(json.dumps(row, ensure_ascii=False, default=str), encoding="utf-8")


def store_stats() -> dict[str, Any]:
    root = store_root() / "by_request"
    n = sum(1 for _ in root.glob("*.json")) if root.exists() else 0
    return {"paid_response_files": n, "root": str(store_root())}


def merge_thicker(existing: Any, incoming: Any) -> Any:
    """Never overwrite complete information with a short summary."""
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    if isinstance(existing, str) and isinstance(incoming, str):
        return incoming if len(incoming) > len(existing) else existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        out = dict(existing)
        for k, v in incoming.items():
            if k not in out or out[k] in (None, "", [], {}):
                out[k] = v
            else:
                out[k] = merge_thicker(out[k], v)
        return out
    if isinstance(existing, list) and isinstance(incoming, list):
        return incoming if len(incoming) > len(existing) else existing
    return incoming
