"""Full solicitation text store — separate from short UI/listing previews (Build 002O).

NextBid listing `summary` is provider-capped at ~500 chars. That preview must never
be the sole extraction input when fuller package/document text exists.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

PREVIEW_CHARS = 500
QTY_SECTION_RE = re.compile(
    r"(?is)(.{0,200}?(?:quantity|qty|line\s*item|bid\s*schedule|pricing\s*schedule|"
    r"unit\s*price|estimated\s*quantity|furnish|purchase\s+of|order\s+quantity|"
    r"items?\s+to\s+be\s+(?:purchased|furnished)|schedule\s+of\s+prices?).{0,800})"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_root() -> Path:
    p = get_settings().data_root() / "solicitation_full_text"
    p.mkdir(parents=True, exist_ok=True)
    return p


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def full_text_path(record_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._\-]+", "_", (record_key or "unknown"))[:120]
    return store_root() / f"{safe}.json"


def store_full_text(
    record_key: str,
    text: str,
    *,
    source: str,
    title: str | None = None,
    preview_chars: int = PREVIEW_CHARS,
    force: bool = False,
) -> dict[str, Any]:
    """Persist complete extracted text; never overwrite thicker matching body with thinner."""
    full = (text or "").replace("\x00", "")
    existing = load_full_text(record_key)
    if existing and not force:
        old = str(existing.get("full_text") or "")
        # Keep longer text unless new text is substantially more relevant and longer
        if len(old) >= len(full) and len(old) >= 80:
            return {
                "record_key": record_key,
                "content_hash": existing.get("content_hash"),
                "full_text_chars": int(existing.get("full_text_chars") or len(old)),
                "preview": (existing.get("preview") or old[:preview_chars]),
                "preview_chars": int(existing.get("preview_chars") or min(len(old), preview_chars)),
                "path": str(full_text_path(record_key)),
                "source": existing.get("source"),
                "skipped_thinner": True,
            }
    h = content_hash(full)
    preview = full[:preview_chars]
    row = {
        "record_key": record_key,
        "title": title,
        "source": source,
        "content_hash": h,
        "full_text_chars": len(full),
        "preview_chars": len(preview),
        "preview": preview,
        "full_text": full,
        "updated_at": _utc(),
        "is_listing_preview_only": len(full) <= preview_chars and source.startswith(
            ("listing", "offline_listing", "permanent_repair_search")
        ),
    }
    path = full_text_path(record_key)
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    return {
        "record_key": record_key,
        "content_hash": h,
        "full_text_chars": len(full),
        "preview": preview,
        "preview_chars": len(preview),
        "path": str(path),
        "source": source,
    }


def load_full_text(record_key: str) -> dict[str, Any] | None:
    path = full_text_path(record_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_relevant_sections(text: str, *, title: str = "", max_chars: int = 24000) -> dict[str, Any]:
    """Local section filter — prefer quantity/schedule language; never invent content."""
    blob = f"{title}\n{text or ''}".replace("\x00", "")
    if len(blob) <= max_chars:
        return {
            "text": blob,
            "chars": len(blob),
            "method": "full_document",
            "sections": 1,
            "truncated": False,
        }
    hits: list[str] = []
    for m in QTY_SECTION_RE.finditer(blob):
        snip = m.group(1).strip()
        if snip and snip not in hits:
            hits.append(snip)
        if sum(len(h) for h in hits) >= max_chars:
            break
    # Always keep title + head + tail for context (amendments often at end)
    head = blob[:4000]
    tail = blob[-4000:] if len(blob) > 8000 else ""
    parts = [f"TITLE: {title}", "[document_head]", head]
    if hits:
        parts.append("[quantity_relevant_sections]")
        parts.extend(hits)
    if tail:
        parts.append("[document_tail]")
        parts.append(tail)
    out = "\n\n".join(parts)
    truncated = len(out) > max_chars
    if truncated:
        out = out[:max_chars]
    return {
        "text": out,
        "chars": len(out),
        "method": "section_filter",
        "sections": len(hits),
        "truncated": truncated,
        "source_chars": len(blob),
    }
