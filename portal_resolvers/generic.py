"""Generic public detail-page document resolution fallback."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from portal_document_resolver import (
    ACTUAL_DOCUMENT_BYTES,
    DETAIL_PAGE,
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    PACKAGE_ROUTE_NOT_FOUND,
    _utc,
    content_fingerprint,
    detect_file_format,
    live_http_get,
)


def resolve_generic_documents(row: dict[str, Any]) -> dict[str, Any]:
    detail = row.get("detail_url") or row.get("source_url") or row.get("url")
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    if not detail:
        return {
            "family": "GENERIC",
            "ok": False,
            "failure": PACKAGE_ROUTE_NOT_FOUND,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
        }
    hit = live_http_get(str(detail), source_id="generic_detail")
    attempts.append({"step": DETAIL_PAGE, "url": detail, "status": hit.get("status_code"), "ok": hit.get("ok"), "at": _utc()})
    if not hit.get("ok"):
        return {
            "family": "GENERIC",
            "ok": False,
            "failure": hit.get("failure") or DOWNLOAD_ENDPOINT_UNRESOLVED,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
        }
    body = hit.get("text") or ""
    for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|docx|xlsx)(?:\?[^"\']*)?)["\']', body, re.I):
        href = m.group(1)
        url = href if href.startswith("http") else urljoin(str(detail), href)
        phit = live_http_get(url, source_id="generic_doc", referer=str(detail))
        attempts.append(
            {
                "step": ACTUAL_DOCUMENT_BYTES,
                "url": url[:180],
                "status": phit.get("status_code"),
                "ok": phit.get("ok"),
                "bytes": len(phit.get("content") or b""),
                "at": _utc(),
            }
        )
        content = phit.get("content") or b""
        fmt = detect_file_format(content, phit.get("content_type"))
        if not fmt.get("ok"):
            continue
        text = ""
        if fmt.get("format") == "PDF":
            try:
                from pdf_text import extract_pdf_text

                text = extract_pdf_text(content) or ""
            except Exception:
                text = ""
        documents.append(
            {
                "url": url.split("?")[0],
                "download_url": url,
                "title": url.rsplit("/", 1)[-1][:120],
                "document_type": "SOLICITATION",
                "format": fmt.get("format"),
                "bytes_recovered": True,
                "byte_count": len(content),
                "content_fingerprint": content_fingerprint(content),
                "text_preview": (text or "")[:8000] or None,
                "extracted_text": (text or "")[:200000] or None,
                "authority": "authoritative",
                "source": "generic_detail",
                "layer": ACTUAL_DOCUMENT_BYTES,
                "retrieved_at": _utc(),
                "validation": fmt.get("reason"),
            }
        )
        if len(documents) >= 3:
            break
    return {
        "family": "GENERIC",
        "ok": bool(documents),
        "failure": None if documents else DOWNLOAD_ENDPOINT_UNRESOLVED,
        "documents": documents,
        "line_items": [],
        "attempts": attempts,
        "detail_url": detail,
        "status": DOCUMENT_BYTES_RECOVERED if documents else DOWNLOAD_ENDPOINT_UNRESOLVED,
    }
