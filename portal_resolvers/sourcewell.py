"""Sourcewell solicitation document resolution.

Public marketing pages describe open RFPs, but the full package lives on
proportal.sourcewell-mn.gov (registration required).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from portal_document_resolver import (
    ACTUAL_DOCUMENT_BYTES,
    DETAIL_PAGE,
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    LOGIN_REQUIRED,
    PACKAGE_ROUTE_NOT_FOUND,
    REGISTRATION_REQUIRED,
    _utc,
    content_fingerprint,
    detect_file_format,
    live_http_get,
)

PROPORTAL = "https://proportal.sourcewell-mn.gov"
REGISTER_URL = "https://www.sourcewell-mn.gov/register"


def resolve_sourcewell_documents(row: dict[str, Any]) -> dict[str, Any]:
    detail = row.get("detail_url") or row.get("source_url") or row.get("url")
    sol = str(row.get("solicitation_number") or row.get("external_id") or "")
    if not detail and sol.isdigit():
        detail = f"https://www.sourcewell-mn.gov/solicitations/{sol}"
    if not detail and "sourcewell" in str(row.get("canonical_id") or ""):
        m = re.search(r"(\d{4,})", str(row.get("canonical_id")))
        if m:
            detail = f"https://www.sourcewell-mn.gov/solicitations/{m.group(1)}"
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    access_state = None
    failure = None
    if not detail:
        return {
            "family": "SOURCEWELL",
            "ok": False,
            "failure": PACKAGE_ROUTE_NOT_FOUND,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
        }

    hit = live_http_get(str(detail), source_id="sourcewell_detail")
    attempts.append({"step": DETAIL_PAGE, "url": detail, "status": hit.get("status_code"), "ok": hit.get("ok"), "at": _utc()})
    if not hit.get("ok"):
        return {
            "family": "SOURCEWELL",
            "ok": False,
            "failure": hit.get("failure") or DOWNLOAD_ENDPOINT_UNRESOLVED,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
            "detail_url": detail,
        }

    body = hit.get("text") or ""
    awarded = bool(re.search(r"\bawarded\b|contract\s+#|vendor\s+awarded", body[:8000], re.I))
    proportal_required = bool(
        re.search(
            r"proportal\.sourcewell|full copy of the RFP|obtain a copy of the complete RFP|only\s+proposals?\s+submitted",
            body,
            re.I,
        )
    )

    # Probe proportal (often login/registration wall)
    ph = live_http_get(PROPORTAL, source_id="sourcewell_proportal")
    attempts.append(
        {
            "step": "proportal",
            "url": PROPORTAL,
            "status": ph.get("status_code"),
            "ok": ph.get("ok"),
            "at": _utc(),
        }
    )
    ptext = (ph.get("text") or "").lower()
    if proportal_required or re.search(r"sign\s*in|log\s*in|register|password", ptext[:4000], re.I):
        access_state = "REGISTRATION_REQUIRED"
        failure = REGISTRATION_REQUIRED

    candidates = []
    for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|docx|xlsx|xls|zip)(?:\?[^"\']*)?)["\']', body, re.I):
        href = m.group(1)
        full = href if href.startswith("http") else urljoin(str(detail), href)
        candidates.append(full)
    for m in re.finditer(r'href=["\']([^"\']*(?:download|file|document|attachment)[^"\']*)["\']', body, re.I):
        href = m.group(1)
        if href.startswith("#") or "javascript:" in href.lower():
            continue
        full = href if href.startswith("http") else urljoin(str(detail), href)
        if full not in candidates and "proportal" not in full.lower():
            candidates.append(full)

    for url in candidates[:6]:
        phit = live_http_get(url, source_id="sourcewell_doc", referer=str(detail))
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
        elif fmt.get("format") == "HTML":
            text = (phit.get("text") or "")[:50000]
        documents.append(
            {
                "url": url.split("?")[0],
                "download_url": url,
                "title": url.rsplit("/", 1)[-1][:120],
                "document_type": "SOLICITATION" if fmt.get("format") == "PDF" else "DETAIL_HTML",
                "format": fmt.get("format"),
                "bytes_recovered": True,
                "byte_count": len(content),
                "content_fingerprint": content_fingerprint(content),
                "text_preview": (text or "")[:8000] or None,
                "extracted_text": (text or "")[:200000] or None,
                "authority": "authoritative",
                "source": "sourcewell_detail",
                "layer": ACTUAL_DOCUMENT_BYTES,
                "retrieved_at": _utc(),
                "validation": fmt.get("reason"),
            }
        )
        if len(documents) >= 3:
            break

    # Public HTML summary (not the governing RFP when proportal is required)
    if body:
        documents.append(
            {
                "url": str(detail),
                "title": row.get("title") or "sourcewell-detail",
                "document_type": "DETAIL_HTML",
                "format": "HTML",
                "bytes_recovered": True,
                "byte_count": len(body.encode("utf-8", errors="ignore")),
                "content_fingerprint": content_fingerprint(body.encode("utf-8", errors="ignore")),
                "text_preview": body[:8000],
                "extracted_text": body[:100000],
                "authority": "secondary",
                "source": "sourcewell_public_summary",
                "layer": DETAIL_PAGE,
                "retrieved_at": _utc(),
                "awarded_or_informational": awarded,
                "validation": DOCUMENT_BYTES_RECOVERED,
            }
        )

    authoritative = [d for d in documents if d.get("authority") == "authoritative" and d.get("format") != "HTML"]
    return {
        "family": "SOURCEWELL",
        "ok": bool(documents),
        "failure": failure if not authoritative else None,
        "documents": documents,
        "line_items": [],
        "attempts": attempts,
        "detail_url": detail,
        "awarded_or_informational": awarded,
        "access_state": access_state,
        "registration_url": REGISTER_URL,
        "login_url": PROPORTAL,
        "retrieval_method": "sourcewell_public_summary_proportal_gated",
        "status": DOCUMENT_BYTES_RECOVERED if authoritative else (failure or DOCUMENT_BYTES_RECOVERED),
        "proportal_required": proportal_required,
    }
