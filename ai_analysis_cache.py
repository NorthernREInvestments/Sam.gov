"""Deterministic analysis cache — identical fingerprints must not re-call OpenAI."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("govtracker.ai.cache")

PROMPT_SCHEMA_VERSION = "funnel-v1"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def hash_source_material(*parts: Any) -> str:
    """Hash relevant source text/document identifiers (not full PDF bodies in the key store)."""
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            continue
        if isinstance(part, bytes):
            h.update(part)
        else:
            h.update(_stable_json(part).encode("utf-8"))
    return h.hexdigest()


def analysis_fingerprint(
    *,
    notice_id: str | None,
    task: str,
    model_tier: str,
    source_hash: str,
    schema_version: str = PROMPT_SCHEMA_VERSION,
    funnel_stage: int | None = None,
) -> str:
    payload = {
        "notice_id": notice_id or "",
        "task": task,
        "model_tier": model_tier,
        "source_hash": source_hash,
        "schema_version": schema_version,
        "funnel_stage": funnel_stage,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _cache_key(fingerprint: str) -> str:
    """
    AppSetting.key is String(64).
    Legacy keys like 'ai_analysis_cache_'+sha256 were 82 chars and silently failed inserts.
    Namespace + hash into exactly 64 hex chars.
    """
    return hashlib.sha256(f"ai_analysis_cache_v1:{fingerprint}".encode("utf-8")).hexdigest()


def get_cached_analysis(fingerprint: str) -> dict[str, Any] | None:
    from database import SessionLocal
    from models import AppSetting

    session = SessionLocal()
    try:
        row = session.query(AppSetting).filter_by(key=_cache_key(fingerprint)).first()
        if not row or not row.value:
            return None
        try:
            data = json.loads(row.value)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and "result_text" in data:
            return data
        return None
    finally:
        session.close()


def put_cached_analysis(
    fingerprint: str,
    *,
    result_text: str,
    meta: dict[str, Any] | None = None,
) -> bool:
    """
    Persist cache row and verify independent read-back.
    Returns True ONLY after commit + confirmed read.
    """
    from database import SessionLocal
    from models import AppSetting

    payload = {
        "fingerprint": fingerprint,
        "result_text": result_text,
        "cached_at": now_utc().isoformat(),
        "meta": meta or {},
    }
    if len(payload["result_text"]) > 200_000:
        payload["result_text"] = payload["result_text"][:200_000]
    serialized = json.dumps(payload)
    key = _cache_key(fingerprint)

    session = SessionLocal()
    try:
        row = session.query(AppSetting).filter_by(key=key).first()
        if row:
            row.value = serialized
        else:
            session.add(AppSetting(key=key, value=serialized))
        session.commit()
    except Exception:
        logger.exception("ai analysis cache write failed")
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()

    # Independent read confirmation (new session)
    verified = get_cached_analysis(fingerprint)
    if not verified or verified.get("result_text") != payload["result_text"]:
        logger.error("ai analysis cache verify failed for fingerprint %s…", fingerprint[:12])
        return False
    return True


def content_source_hash(content: list[dict[str, Any]], instructions: str | None = None) -> str:
    """Fingerprint dynamic content; exclude volatile timestamps from static instructions."""
    digest_parts: list[Any] = []
    if instructions:
        digest_parts.append({"instructions_sha": hashlib.sha256(instructions.encode("utf-8")).hexdigest()})
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"input_text", "text"}:
            digest_parts.append({"t": kind, "text": part.get("text") or ""})
        elif kind in {"input_file", "document"}:
            file_data = str(part.get("file_data") or "")
            src = part.get("source") if isinstance(part.get("source"), dict) else {}
            data_b64 = str(src.get("data") or "")
            blob = file_data or data_b64
            digest_parts.append(
                {
                    "t": kind,
                    "filename": part.get("filename") or "",
                    "sha": hashlib.sha256(blob.encode("ascii", errors="ignore")).hexdigest() if blob else "",
                    "n": len(blob),
                }
            )
        elif kind in {"input_image", "image"}:
            url = str(part.get("image_url") or "")
            digest_parts.append({"t": kind, "sha": hashlib.sha256(url.encode("utf-8")).hexdigest(), "n": len(url)})
        else:
            digest_parts.append(part)
    return hash_source_material(*digest_parts)
