"""Shared attachment extraction from HTML / JSON / embedded document lists.

Public, official-source only. No auth bypass, CAPTCHA defeat, or aggressive scrape.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

FILE_EXT_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|csv|zip|txt|rtf)(?:\?|$|#)",
    re.I,
)
HREF_FILE_RE = re.compile(
    r"""(?:href|data-href|data-url|data-download-url)\s*=\s*["']([^"']+)["']""",
    re.I,
)
DOWNLOAD_HINT_RE = re.compile(
    r"(?:download|attachment|document|file|bid[\s_-]?package|solicitation|addendum|amendment)",
    re.I,
)
JSON_URL_KEYS = (
    "url",
    "href",
    "downloadUrl",
    "download_url",
    "fileUrl",
    "file_url",
    "documentUrl",
    "document_url",
    "link",
    "uri",
    "path",
)
JSON_NAME_KEYS = ("name", "title", "fileName", "filename", "description", "label", "documentName")
JSON_ID_KEYS = ("id", "attachmentId", "attachment_id", "resourceId", "resource_id", "documentId", "fileId")

_DOC_TYPE_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("pricing_schedule", re.compile(r"pric(e|ing)\s*(schedule|sheet|list|table)", re.I)),
    ("spreadsheet", re.compile(r"\.(xlsx?|csv)\b|spreadsheet|line[\s_-]?item", re.I)),
    ("bom", re.compile(r"\bbom\b|bill\s+of\s+materials|parts\s+list", re.I)),
    ("technical_specification", re.compile(r"spec(ification)?s?|technical\s+data|drawing", re.I)),
    ("award_notice", re.compile(r"award\s+notice|award\s+of\s+contract", re.I)),
    ("historical_award_document", re.compile(r"historical\s+award|prior\s+award", re.I)),
    ("amendment", re.compile(r"amendment|addendum|modification", re.I)),
    ("solicitation", re.compile(r"solicitation|invitation\s+for\s+bid|IFB|RFQ|RFP|combined\s+synopsis", re.I)),
    ("attachment", re.compile(r"attachment|exhibit|appendix", re.I)),
]


def classify_document_type(name: str, url: str = "", meta: dict[str, Any] | None = None) -> str:
    meta = meta or {}
    explicit = str(meta.get("document_type") or meta.get("type") or "").lower().strip()
    if explicit:
        return explicit
    hay = f"{name} {url} {meta.get('description') or ''}"
    for dtype, pat in _DOC_TYPE_HINTS:
        if pat.search(hay):
            return dtype
    if str(url).lower().endswith((".xlsx", ".xls", ".csv")):
        return "spreadsheet"
    if str(url).lower().endswith(".pdf"):
        return "attachment"
    return "unknown"


def _basename(url: str) -> str:
    path = urlparse(url).path or url
    return (path.rstrip("/").rsplit("/", 1)[-1] or "document")[:180]


def _looks_like_file_url(url: str) -> bool:
    u = (url or "").lower()
    if not u or u.startswith("javascript:") or u.startswith("mailto:"):
        return False
    if FILE_EXT_RE.search(u):
        return True
    if any(x in u for x in ("/download", "/attachment", "/file/", "resource", "document")):
        return True
    return False


def classify_access_from_html(html: str) -> str | None:
    text = (html or "").lower()
    if not text:
        return None
    if any(x in text for x in ("captcha", "cloudflare", "cf-challenge", "bot detection", "attention required")):
        return "ACCESS_BLOCKED"
    if any(
        x in text
        for x in (
            "create an account",
            "vendor registration",
            "supplier registration",
            "must register",
            "registration required",
        )
    ):
        return "REGISTRATION_REQUIRED"
    if any(x in text for x in ("sign in", "log in", "login", "please authenticate", "password")):
        return "AUTH_REQUIRED"
    return None


def extract_file_hrefs(html: str, *, base_url: str | None = None) -> list[dict[str, Any]]:
    """Extract candidate document URLs from HTML download sections / tables."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    body = html or ""
    for m in HREF_FILE_RE.finditer(body):
        href = (m.group(1) or "").strip()
        if not href or href.startswith("#"):
            continue
        url = href if href.startswith("http") else (urljoin(base_url or "", href) if base_url else href)
        if not _looks_like_file_url(url):
            # Keep if surrounding context looks like a document download control
            start = max(0, m.start() - 120)
            end = min(len(body), m.end() + 120)
            if not DOWNLOAD_HINT_RE.search(body[start:end]):
                continue
        key = url.split("?")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        name = _basename(url)
        found.append(
            {
                "url": url.split("#")[0],
                "name": name,
                "document_type": classify_document_type(name, url),
                "attachment_id": None,
                "source": "html_href",
            }
        )
    # Document tables: filename in cell + link nearby
    for m in re.finditer(
        r"(?:Attachment|Document|File|Addendum|Amendment)\s*[:#]?\s*"
        r"([^<\n]{3,80}).{0,200}?href=[\"']([^\"']+)[\"']",
        body,
        re.I | re.S,
    ):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -:.")
        href = m.group(2).strip()
        url = href if href.startswith("http") else (urljoin(base_url or "", href) if base_url else href)
        key = url.split("?")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "url": url.split("#")[0],
                "name": name[:180] or _basename(url),
                "document_type": classify_document_type(name, url),
                "attachment_id": None,
                "source": "html_document_table",
            }
        )
    return found


def _walk_json_for_docs(node: Any, *, out: list[dict[str, Any]], seen: set[str], depth: int = 0) -> None:
    if depth > 8 or len(out) >= 80:
        return
    if isinstance(node, list):
        for item in node[:100]:
            _walk_json_for_docs(item, out=out, seen=seen, depth=depth + 1)
        return
    if not isinstance(node, dict):
        return
    url = None
    for k in JSON_URL_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip() and (_looks_like_file_url(v) or "download" in v.lower()):
            url = v.strip()
            break
    if url:
        key = url.split("?")[0].lower()
        if key not in seen:
            seen.add(key)
            name = None
            for nk in JSON_NAME_KEYS:
                if isinstance(node.get(nk), str) and node.get(nk).strip():
                    name = str(node.get(nk)).strip()
                    break
            att_id = None
            for ik in JSON_ID_KEYS:
                if node.get(ik) not in {None, ""}:
                    att_id = str(node.get(ik))
                    break
            name = name or _basename(url)
            out.append(
                {
                    "url": url,
                    "name": name[:180],
                    "document_type": classify_document_type(name, url, node),
                    "attachment_id": att_id,
                    "source": "json_document_list",
                    "meta": {k: node.get(k) for k in ("mimeType", "size", "postedDate", "type") if node.get(k)},
                }
            )
    for v in node.values():
        if isinstance(v, (dict, list)):
            _walk_json_for_docs(v, out=out, seen=seen, depth=depth + 1)


def extract_docs_from_json(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    _walk_json_for_docs(payload, out=out, seen=seen)
    return out


def extract_embedded_json_docs(html: str) -> list[dict[str, Any]]:
    """Pull document lists from script JSON blobs commonly embedded in portal pages."""
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"<script[^>]*>(\{.*?\"(?:downloadUrl|download_url|attachments|documents|files)\".*?\})</script>",
        html or "",
        re.I | re.S,
    ):
        blob = m.group(1)
        try:
            payload = json.loads(blob)
        except Exception:
            continue
        for d in extract_docs_from_json(payload):
            key = str(d.get("url") or "").split("?")[0].lower()
            if key and key not in seen:
                seen.add(key)
                docs.append(d)
        if len(docs) >= 40:
            break
    return docs


def extract_attachment_candidates(
    *,
    html: str | None = None,
    json_payload: Any = None,
    base_url: str | None = None,
    existing_links: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Unified attachment candidate discovery from all public surfaces."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(item: dict[str, Any]) -> None:
        url = str(item.get("url") or item.get("download_url") or "").strip()
        if not url:
            return
        key = url.split("?")[0].lower()
        if key in seen:
            return
        seen.add(key)
        name = str(item.get("name") or item.get("title") or item.get("filename") or _basename(url))
        found.append(
            {
                "url": url,
                "name": name[:180],
                "document_type": item.get("document_type")
                or classify_document_type(name, url, item if isinstance(item, dict) else None),
                "attachment_id": item.get("attachment_id") or item.get("resource_id"),
                "source": item.get("source") or "candidate",
                "meta": item.get("meta") or {},
            }
        )

    for link in existing_links or []:
        if isinstance(link, str):
            _add({"url": link, "source": "existing_link"})
        elif isinstance(link, dict):
            _add({**link, "source": link.get("source") or "existing_link"})

    if json_payload is not None:
        for d in extract_docs_from_json(json_payload):
            _add(d)

    if html:
        for d in extract_file_hrefs(html, base_url=base_url):
            _add(d)
        for d in extract_embedded_json_docs(html):
            _add(d)

    return found


def fetch_document_bytes(
    url: str,
    *,
    source_id: str,
    referer: str | None = None,
    document_type: str | None = None,
    title: str | None = None,
    authority: str = "authoritative",
    max_docs_guard: bool = True,  # noqa: ARG001 — reserved
) -> dict[str, Any] | None:
    """Download and validate a single public document URL into resolver document shape."""
    from portal_document_resolver import (
        ACTUAL_DOCUMENT_BYTES,
        content_fingerprint,
        detect_file_format,
        live_http_get,
        _utc,
    )

    hit = live_http_get(url, source_id=source_id, referer=referer)
    content = hit.get("content") or b""
    fmt = detect_file_format(content, hit.get("content_type"))
    attempt = {
        "step": ACTUAL_DOCUMENT_BYTES,
        "url": url[:220],
        "status": hit.get("status_code"),
        "ok": hit.get("ok") and fmt.get("ok"),
        "bytes": len(content),
        "failure": hit.get("failure") or (None if fmt.get("ok") else fmt.get("reason")),
        "at": _utc(),
    }
    if not hit.get("ok") or not fmt.get("ok"):
        return {"attempt": attempt, "document": None, "access": classify_access_from_html(hit.get("text") or "")}

    text = ""
    if fmt.get("format") == "PDF":
        try:
            from pdf_text import extract_pdf_text

            text = extract_pdf_text(content) or ""
        except Exception:
            text = ""
    elif fmt.get("format") in {"TXT", "HTML"}:
        text = (hit.get("text") or content.decode("utf-8", errors="ignore"))[:200000]

    name = title or _basename(url)
    doc = {
        "url": url.split("?")[0],
        "download_url": url,
        "title": name[:120],
        "name": name[:120],
        "document_type": (document_type or classify_document_type(name, url)).upper()
        if (document_type or "").isupper()
        else (document_type or classify_document_type(name, url)),
        "format": fmt.get("format"),
        "bytes_recovered": True,
        "byte_count": len(content),
        "content_fingerprint": content_fingerprint(content),
        "text_preview": (text or "")[:8000] or None,
        "extracted_text": (text or "")[:200000] or None,
        "authority": authority,
        "source": source_id,
        "layer": ACTUAL_DOCUMENT_BYTES,
        "retrieved_at": _utc(),
        "validation": fmt.get("reason"),
        "status": "RECOVERED",
    }
    return {"attempt": attempt, "document": doc, "access": None}
