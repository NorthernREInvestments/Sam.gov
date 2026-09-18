"""SAM / Federal description recovery — resolve URL descriptions without bypass."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from application_clock import now_utc
from federal_dla_product_constants import (
    DESCRIPTION_AUTH_REQUIRED,
    DESCRIPTION_BOT_BLOCKED,
    DESCRIPTION_EMPTY,
    DESCRIPTION_EXTERNAL_POINTER,
    DESCRIPTION_FETCH_FAILED,
    DESCRIPTION_INLINE,
    DESCRIPTION_NOT_FOUND,
    DESCRIPTION_PUBLIC_RECOVERED,
    DESCRIPTION_UNSUPPORTED,
)

_URL_RE = re.compile(r"^https?://", re.I)
_HTML_STRIP = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _utc() -> str:
    return now_utc().isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def classify_description_field(raw: Any) -> dict[str, Any]:
    """Classify raw SAM description without network I/O."""
    if raw is None:
        return {"kind": DESCRIPTION_EMPTY, "is_url": False, "url": None, "inline_text": None}
    if isinstance(raw, dict):
        url = raw.get("url") or raw.get("href")
        text = raw.get("text") or raw.get("body")
        if url and _URL_RE.match(str(url)):
            return {"kind": DESCRIPTION_EXTERNAL_POINTER, "is_url": True, "url": str(url).strip(), "inline_text": text}
        if text:
            return {"kind": DESCRIPTION_INLINE, "is_url": False, "url": None, "inline_text": str(text)}
        return {"kind": DESCRIPTION_EMPTY, "is_url": False, "url": None, "inline_text": None}
    s = str(raw).strip()
    if not s:
        return {"kind": DESCRIPTION_EMPTY, "is_url": False, "url": None, "inline_text": None}
    if _URL_RE.match(s) and len(s) < 2000 and "\n" not in s[:200]:
        return {"kind": DESCRIPTION_EXTERNAL_POINTER, "is_url": True, "url": s, "inline_text": None}
    return {"kind": DESCRIPTION_INLINE, "is_url": False, "url": None, "inline_text": s}


def _normalize_html_or_text(data: bytes, content_type: str) -> str:
    ctype = (content_type or "").lower()
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    # SAM noticedesc returns {"description": "..."}
    if "json" in ctype or (text[:1] == "{" and "description" in text[:200]):
        try:
            import json

            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("description"):
                text = str(obj["description"])
            elif isinstance(obj, dict) and obj.get("data"):
                text = str(obj.get("data"))
        except Exception:
            pass
    if "html" in ctype or "<html" in text[:2000].lower() or "<body" in text[:2000].lower():
        text = _HTML_STRIP.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:50000]


def recover_description(
    row: dict[str, Any],
    *,
    authorize_live: bool = False,
    known_hash: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """
    Resolve description for one Federal/DLA opportunity.
    Never bypasses auth/bot. Preserves raw metadata + provenance.
    """
    raw_meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    # Prefer explicit description URL from links / raw
    candidate = row.get("description")
    desc_url = None
    for link in row.get("document_links") or []:
        if isinstance(link, dict) and str(link.get("kind") or "") in {"sam_description", "description"}:
            desc_url = link.get("url")
            break
        if isinstance(link, dict) and "noticedesc" in str(link.get("url") or "").lower():
            desc_url = link.get("url")
            break
    if not desc_url and isinstance(candidate, str) and _URL_RE.match(candidate.strip()):
        desc_url = candidate.strip()
        candidate = None
    # Pipeline handoff often strips description URL — synthesize official noticedesc from notice id
    if not desc_url and (not candidate or not str(candidate).strip()):
        nid = str(row.get("notice_id") or row.get("external_id") or "").strip()
        if nid and len(nid) >= 16 and " " not in nid:
            desc_url = f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={nid}"
            candidate = None

    classified = classify_description_field(desc_url or candidate)
    out: dict[str, Any] = {
        "kind": "FEDERAL_DESCRIPTION_RECOVERY",
        "description_state": classified["kind"],
        "source_url": classified.get("url"),
        "retrieved_at": None,
        "http_status": None,
        "content_type": None,
        "content_hash": None,
        "text": None,
        "bytes": 0,
        "skipped_unchanged": False,
        "bypass_attempted": False,
        "provenance": {
            "notice_id": row.get("notice_id") or row.get("external_id"),
            "solicitation_number": row.get("solicitation_number"),
            "raw_description_preserved": True,
        },
    }

    if classified["kind"] == DESCRIPTION_INLINE:
        text = str(classified.get("inline_text") or "")
        h = _sha256_text(text)
        out.update(
            {
                "description_state": DESCRIPTION_INLINE,
                "text": text[:50000],
                "content_hash": h,
                "retrieved_at": _utc(),
                "skipped_unchanged": bool(known_hash and known_hash == h),
            }
        )
        return out

    if classified["kind"] == DESCRIPTION_EMPTY:
        out["description_state"] = DESCRIPTION_EMPTY
        return out

    url = classified.get("url")
    if not url:
        out["description_state"] = DESCRIPTION_UNSUPPORTED
        return out

    # External portal pointers that are clearly not SAM description APIs
    host = (urlparse(url).hostname or "").lower()
    if any(x in host for x in ("dibbs.bsm.dla.mil", "piee.eb.mil", "cfolders", "tdmt")):
        out["description_state"] = DESCRIPTION_EXTERNAL_POINTER
        out["source_url"] = url
        return out

    if not authorize_live:
        out["description_state"] = DESCRIPTION_EXTERNAL_POINTER
        out["error"] = "authorize_live_required"
        return out

    try:
        import httpx
        from direct_document_retrieval import _with_api_key_if_sam_file

        fetch_url = _with_api_key_if_sam_file(url)
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0), follow_redirects=True) as client:
            resp = client.get(fetch_url)
        out["http_status"] = resp.status_code
        out["retrieved_at"] = _utc()
        out["content_type"] = (resp.headers.get("content-type") or "").split(";")[0].strip()
        out["source_url"] = url

        if resp.status_code in {401, 403}:
            # Distinguish soft bot vs auth when possible
            body_l = (resp.text or "")[:2000].lower()
            if any(t in body_l for t in ("captcha", "cloudflare", "bot detection", "access denied")):
                out["description_state"] = DESCRIPTION_BOT_BLOCKED
            else:
                out["description_state"] = DESCRIPTION_AUTH_REQUIRED
            return out
        if resp.status_code == 404:
            out["description_state"] = DESCRIPTION_NOT_FOUND
            return out
        if resp.status_code >= 400:
            out["description_state"] = DESCRIPTION_FETCH_FAILED
            out["error"] = f"http_{resp.status_code}"
            return out

        data = resp.content or b""
        out["bytes"] = len(data)
        # Never persist api_key in provenance
        safe_url = url
        try:
            p = urlparse(str(resp.url) if resp.url else url)
            q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() != "api_key"]
            safe_url = urlunparse(p._replace(query=urlencode(q)))
        except Exception:
            safe_url = url
        out["source_url"] = safe_url
        if len(data) > 5_000_000:
            out["description_state"] = DESCRIPTION_UNSUPPORTED
            out["error"] = "payload_too_large"
            return out
        text = _normalize_html_or_text(data, out["content_type"] or "")
        if not text or len(text) < 20:
            out["description_state"] = DESCRIPTION_FETCH_FAILED
            out["error"] = "empty_or_unusable_body"
            return out
        h = _sha256_text(text)
        out["content_hash"] = h
        if known_hash and known_hash == h:
            out["skipped_unchanged"] = True
            out["description_state"] = DESCRIPTION_PUBLIC_RECOVERED
            out["text"] = None  # caller already has it
            return out
        out["text"] = text
        out["description_state"] = DESCRIPTION_PUBLIC_RECOVERED
        return out
    except Exception as exc:  # noqa: BLE001
        out["description_state"] = DESCRIPTION_FETCH_FAILED
        out["error"] = str(exc)[:300]
        out["retrieved_at"] = _utc()
        return out


def apply_description_recovery(row: dict[str, Any], recovery: dict[str, Any]) -> dict[str, Any]:
    """Merge recovery into opportunity without destroying original metadata."""
    out = dict(row)
    meta = dict(out.get("raw_metadata") or {})
    meta["original_description"] = meta.get("original_description", out.get("description"))
    meta["description_recovery"] = {
        k: recovery.get(k)
        for k in (
            "description_state",
            "source_url",
            "retrieved_at",
            "http_status",
            "content_type",
            "content_hash",
            "bytes",
            "skipped_unchanged",
            "error",
        )
    }
    out["raw_metadata"] = meta
    out["description_state"] = recovery.get("description_state")
    out["description_content_hash"] = recovery.get("content_hash")
    text = recovery.get("text")
    if text:
        out["description"] = text
        out["description_recovered"] = True
    elif recovery.get("description_state") == DESCRIPTION_INLINE and out.get("description"):
        out["description_recovered"] = True
    else:
        out["description_recovered"] = bool(recovery.get("description_state") == DESCRIPTION_PUBLIC_RECOVERED)
    # Keep document link for provenance
    links = list(out.get("document_links") or [])
    url = recovery.get("source_url")
    if url and not any(isinstance(l, dict) and l.get("url") == url for l in links):
        links.append({"url": url, "kind": "sam_description", "state": recovery.get("description_state")})
        out["document_links"] = links
    return out
