"""Public document retrieval with hash dedupe and audit — never SAM search API."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from direct_document_retrieval import (
    _filename_from_response,
    _with_api_key_if_sam_file,
    collect_stored_public_urls,
)
from package_manifest import (
    URL_API_ENDPOINT,
    URL_DIRECT_DOCUMENT,
    URL_NOTICE_PAGE,
    URL_RESOURCE_PAGE,
    URL_UNKNOWN,
    classify_url,
)

MAX_BYTES = 25 * 1024 * 1024  # 25 MiB
DEFAULT_TIMEOUT = 60.0


def inventory_urls(contract: Any, *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    """Inventory every locally known URL; classify; no network."""
    items = collect_stored_public_urls(contract)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        url = it.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        kind = classify_url(url)
        out.append(
            {
                "url": url,
                "classification": kind,
                "source_kind": it.get("kind"),
                "retrievable_without_sam_search": kind == URL_DIRECT_DOCUMENT,
                "sam_api_endpoint": kind == URL_API_ENDPOINT,
            }
        )
    if getattr(contract, "link", None):
        u = contract.link
        if u and u not in seen:
            seen.add(u)
            out.append(
                {
                    "url": u,
                    "classification": classify_url(u),
                    "source_kind": "contract.link",
                    "retrievable_without_sam_search": False,
                }
            )
    for u in extra_urls or []:
        if u and u not in seen:
            seen.add(u)
            out.append(
                {
                    "url": u,
                    "classification": classify_url(u),
                    "source_kind": "extra",
                    "retrievable_without_sam_search": classify_url(u) == URL_DIRECT_DOCUMENT,
                }
            )
    return out


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def retrieve_public_document(
    url: str,
    *,
    known_hashes: set[str] | None = None,
    known_urls: set[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """
    Safe HTTP GET for a public/direct document URL.
    Dedupes by URL and hash. Does not call SAM search/enrich APIs.
    """
    audit: dict[str, Any] = {
        "url": url,
        "classification": classify_url(url),
        "retrieved_at": now_utc().isoformat(),
        "sam_search_api": False,
        "LIVE_SAM_API": 0,
    }
    if classify_url(url) == URL_API_ENDPOINT:
        audit.update({"ok": False, "error": "refused_sam_api_endpoint", "skipped": True})
        return audit
    if known_urls and url in known_urls:
        audit.update({"ok": False, "error": "duplicate_url_skipped", "skipped": True})
        return audit
    if classify_url(url) not in {URL_DIRECT_DOCUMENT, URL_RESOURCE_PAGE, URL_UNKNOWN}:
        # Notice pages: do not treat HTML workspace as solicitation PDF
        if classify_url(url) == URL_NOTICE_PAGE:
            audit.update({"ok": False, "error": "notice_page_not_auto_downloaded", "skipped": True})
            return audit

    fetch_url = _with_api_key_if_sam_file(url)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", fetch_url) as resp:
                audit["http_status"] = resp.status_code
                audit["final_url"] = str(resp.url)
                if resp.status_code >= 400:
                    audit.update({"ok": False, "error": f"http_{resp.status_code}"})
                    return audit
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        audit.update({"ok": False, "error": "size_limit_exceeded", "bytes_seen": total})
                        return audit
                    chunks.append(chunk)
                data = b"".join(chunks)
    except Exception as exc:
        audit.update({"ok": False, "error": str(exc)[:300]})
        return audit

    h = content_sha256(data)
    audit["hash"] = h
    if known_hashes and h in known_hashes:
        audit.update({"ok": False, "error": "duplicate_hash_skipped", "skipped": True, "hash": h})
        return audit

    # Content-type / magic validation
    is_pdf = data.startswith(b"%PDF")
    is_html = b"<html" in data[:2000].lower() or "text/html" in ctype
    if not (is_pdf or is_html or ctype.startswith("text/") or "octet-stream" in ctype or "pdf" in ctype):
        audit.update({"ok": False, "error": f"unsupported_content_type:{ctype}"})
        return audit

    path = urlparse(url).path.rstrip("/").split("/")[-1] or "document.bin"
    if path.lower() == "download" or "." not in path:
        filename = "solicitation.pdf" if is_pdf else ("page.html" if is_html else "document.bin")
    else:
        filename = path

    text = ""
    if is_pdf:
        from pdf_text import extract_pdf_text

        text = extract_pdf_text(data) or ""
    elif is_html:
        from sam_enrich import html_to_text

        text = html_to_text(data.decode("utf-8", errors="ignore"))
    else:
        text = data.decode("utf-8", errors="ignore")

    audit.update(
        {
            "ok": True,
            "filename": filename,
            "content_type": ctype or ("application/pdf" if is_pdf else "application/octet-stream"),
            "file_size_bytes": len(data),
            "extracted_text_chars": len(text),
            "bytes": data,
            "text": text,
        }
    )
    return audit
