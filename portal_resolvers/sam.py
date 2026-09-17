"""SAM.gov portal family adapter — attachment / solicitation package recovery.

Uses official SAM resources API metadata + public download URLs.
Does not bypass auth, CAPTCHAs, or scarcity policy gates.
"""

from __future__ import annotations

import re
from typing import Any

from portal_document_resolver import (
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    LOGIN_REQUIRED,
    PACKAGE_ROUTE_NOT_FOUND,
    _utc,
)
from portal_resolvers.attachment_extract import (
    classify_document_type,
    extract_attachment_candidates,
    fetch_document_bytes,
)

NOTICE_ID_RE = re.compile(
    r"(?:notice[_-]?id[=/]|oppId[=/]|/opp/|/opportunities/)([A-Za-z0-9]{10,})",
    re.I,
)
UUIDISH_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)


def extract_sam_notice_id(row: dict[str, Any]) -> str | None:
    for key in ("notice_id", "noticeId", "external_id", "solicitation_number", "canonical_id"):
        v = row.get(key)
        if isinstance(v, str) and len(v.strip()) >= 8:
            # Prefer UUID-like SAM notice ids
            m = UUIDISH_RE.search(v)
            if m:
                return m.group(1)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    for key in ("noticeId", "notice_id", "opportunityId"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    url = str(row.get("detail_url") or row.get("source_url") or row.get("url") or "")
    m = UUIDISH_RE.search(url) or NOTICE_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


def _normalize_sam_attachment(item: dict[str, Any]) -> dict[str, Any] | None:
    url = item.get("download_url") or item.get("url")
    if not url:
        return None
    name = str(item.get("description") or item.get("name") or "SAM Attachment")
    return {
        "url": str(url),
        "name": name,
        "document_type": classify_document_type(name, str(url), item),
        "attachment_id": item.get("resource_id") or item.get("attachment_id"),
        "source": "sam_api_resources",
        "meta": {
            "mime_type": item.get("mime_type"),
            "size": item.get("size"),
            "posted_date": item.get("posted_date"),
            "type": item.get("type"),
        },
    }


def resolve_sam_documents(row: dict[str, Any]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    missing: list[str] = []
    notice_id = extract_sam_notice_id(row)
    detail = row.get("detail_url") or row.get("source_url") or row.get("url")

    if not notice_id and not detail:
        return {
            "family": "SAM",
            "ok": False,
            "failure": PACKAGE_ROUTE_NOT_FOUND,
            "access_status": "UNKNOWN",
            "documents": [],
            "documents_missing": ["solicitation", "attachments"],
            "line_items": [],
            "attempts": attempts,
            "retrieval_method": "sam_resources_api",
        }

    candidates: list[dict[str, Any]] = []

    # Existing discovery refs (resourceLinks / attachment metadata)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    existing = []
    for key in ("resourceLinks", "attachmentDownloadUrls", "document_links", "attachments"):
        val = meta.get(key) or row.get(key)
        if isinstance(val, list):
            existing.extend(val)
    for d in row.get("documents") or []:
        if isinstance(d, dict) and (d.get("url") or d.get("download_url")) and not d.get("bytes_recovered"):
            existing.append(d)
    candidates.extend(
        extract_attachment_candidates(existing_links=existing, base_url=str(detail) if detail else None)
    )

    # Official SAM resources API
    api_failed = False
    api_auth = False
    if notice_id:
        attempts.append({"step": "SAM_RESOURCES_API", "notice_id": notice_id, "at": _utc()})
        try:
            from sam_enrich import fetch_opportunity_attachments

            attachments = fetch_opportunity_attachments(notice_id)
            if attachments is None:
                api_failed = True
                attempts.append({"step": "SAM_RESOURCES_API", "ok": False, "failure": "api_failed", "at": _utc()})
            else:
                attempts.append(
                    {
                        "step": "SAM_RESOURCES_API",
                        "ok": True,
                        "count": len(attachments),
                        "at": _utc(),
                    }
                )
                for item in attachments:
                    if not isinstance(item, dict):
                        continue
                    norm = _normalize_sam_attachment(item)
                    if norm:
                        candidates.append(norm)
        except Exception as exc:  # noqa: BLE001
            api_failed = True
            attempts.append({"step": "SAM_RESOURCES_API", "ok": False, "error": str(exc)[:200], "at": _utc()})

    # Deduplicate candidates
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for c in candidates:
        key = str(c.get("url") or "").split("?")[0].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    access_status = "UNKNOWN"
    for cand in uniq[:8]:
        result = fetch_document_bytes(
            str(cand["url"]),
            source_id="sam_attachment",
            referer=str(detail) if detail else "https://sam.gov/",
            document_type=str(cand.get("document_type") or "attachment"),
            title=str(cand.get("name") or "SAM Attachment"),
        )
        if not result:
            continue
        attempts.append(result["attempt"])
        if result.get("access") == "AUTH_REQUIRED":
            api_auth = True
            access_status = "AUTH_REQUIRED"
        if result.get("document"):
            doc = result["document"]
            doc["attachment_id"] = cand.get("attachment_id")
            doc["source"] = "sam_gov"
            documents.append(doc)

    if not documents and not uniq:
        missing = ["solicitation", "attachments", "amendments"]
        if api_auth:
            status = "AUTH_REQUIRED"
            failure = LOGIN_REQUIRED
        elif api_failed:
            status = "PORTAL_ERROR"
            failure = DOWNLOAD_ENDPOINT_UNRESOLVED
        else:
            status = "NO_DOCUMENTS_AVAILABLE"
            failure = DOWNLOAD_ENDPOINT_UNRESOLVED
        return {
            "family": "SAM",
            "ok": False,
            "failure": failure,
            "access_status": status,
            "access_state": status if status in {"AUTH_REQUIRED"} else None,
            "documents": [],
            "documents_missing": missing,
            "line_items": [],
            "attempts": attempts,
            "notice_id": notice_id,
            "detail_url": detail,
            "retrieval_method": "sam_resources_api",
            "status": status,
        }

    if not documents and uniq:
        # Links found but bytes not recovered — still useful discovery result
        stub_docs = []
        for c in uniq[:12]:
            stub_docs.append(
                {
                    "url": c["url"].split("?")[0],
                    "download_url": c["url"],
                    "title": c.get("name"),
                    "name": c.get("name"),
                    "document_type": c.get("document_type"),
                    "bytes_recovered": False,
                    "authority": "authoritative",
                    "source": "sam_gov_metadata",
                    "attachment_id": c.get("attachment_id"),
                    "retrieved_at": _utc(),
                    "status": "LINKED_UNFETCHED",
                }
            )
        return {
            "family": "SAM",
            "ok": True,
            "failure": None,
            "access_status": "DOCUMENTS_FOUND",
            "documents": stub_docs,
            "documents_missing": [],
            "line_items": [],
            "attempts": attempts,
            "notice_id": notice_id,
            "detail_url": detail,
            "retrieval_method": "sam_resources_api",
            "status": "DOCUMENTS_FOUND",
            "bytes_pending": True,
        }

    return {
        "family": "SAM",
        "ok": True,
        "failure": None,
        "access_status": "DOCUMENTS_FOUND",
        "documents": documents,
        "documents_missing": missing,
        "line_items": [],
        "attempts": attempts,
        "notice_id": notice_id,
        "detail_url": detail,
        "retrieval_method": "sam_resources_api",
        "status": DOCUMENT_BYTES_RECOVERED,
    }
