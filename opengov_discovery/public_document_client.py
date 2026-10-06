"""OpenGovPublicDocumentClient — public project document recovery (no API key / membership).

Proven route (inspected from live portal network + API):
  GET https://api.procurement.opengov.com/api/v1/project/{project_id}

Returns project metadata with:
  attachments[]          — signed S3 download URLs
  documentAttachment     — project document snapshot PDF
  addendums[]
  proposalDocuments / priceTables (when present)

Statuses:
  DOCUMENTS_FOUND_PUBLIC
  NO_PUBLIC_DOCUMENTS
  DOCUMENT_ACCESS_REQUIRES_AUTH
  PROJECT_NOT_FOUND
  RETRYABLE_ERROR
  INVALID_PROJECT
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import httpx

from application_clock import now_utc

log = logging.getLogger("govtracker.opengov_discovery.public_document_client")

API_ROOT = "https://api.procurement.opengov.com/api/v1"
PROJECT_DETAIL_TMPL = f"{API_ROOT}/project/{{project_id}}"

DOCUMENTS_FOUND_PUBLIC = "DOCUMENTS_FOUND_PUBLIC"
NO_PUBLIC_DOCUMENTS = "NO_PUBLIC_DOCUMENTS"
DOCUMENT_ACCESS_REQUIRES_AUTH = "DOCUMENT_ACCESS_REQUIRES_AUTH"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
RETRYABLE_ERROR = "RETRYABLE_ERROR"
INVALID_PROJECT = "INVALID_PROJECT"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_PRICING = re.compile(
    r"(pricing|bid[\s_-]*sheet|bid[\s_-]*schedule|price[\s_-]*schedule|unit[\s_-]*price|"
    r"cost[\s_-]*sheet|quote[\s_-]*sheet|bid[\s_-]*form|proposal[\s_-]*form|"
    r"schedule[\s_-]*of[\s_-]*values)",
    re.I,
)
_SPEC = re.compile(r"\b(spec(?:ification)?s?|scope\s*of\s*work|SOW|technical\s*spec)\b", re.I)
_ADDENDA = re.compile(r"\b(addend(?:a|um)|amendment|clarification)\b", re.I)
_BID_FORM = re.compile(r"\b(bid\s*form|proposal\s*form|response\s*form|signature\s*page)\b", re.I)


def _headers(government_code: str | None, project_id: str | int | None) -> dict[str, str]:
    code = (government_code or "portal").strip().lower() or "portal"
    pid = str(project_id or "").strip()
    referer = (
        f"https://procurement.opengov.com/portal/{code}/projects/{pid}"
        if pid
        else "https://procurement.opengov.com/"
    )
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://procurement.opengov.com",
        "Referer": referer,
    }


def _doc_type_guess(name: str, att_type: str | None = None) -> str:
    blob = f"{name} {att_type or ''}"
    if _ADDENDA.search(blob) or (att_type or "").lower() in {"addendum", "addenda"}:
        return "addenda"
    if _PRICING.search(blob):
        return "pricing_schedule"
    if _SPEC.search(blob):
        return "specifications"
    if _BID_FORM.search(blob):
        return "bid_form"
    if (att_type or "").lower() in {"projectdocument", "project_document"}:
        return "solicitation_packet"
    return "attachment"


def _normalize_attachment(item: dict[str, Any], *, role: str) -> dict[str, Any] | None:
    url = str(item.get("url") or item.get("downloadUrl") or item.get("href") or "").strip()
    if not url.startswith("http"):
        return None
    name = (
        item.get("filename")
        or item.get("name")
        or item.get("title")
        or url.split("?", 1)[0].rsplit("/", 1)[-1]
        or "document.bin"
    )
    name = str(name)
    ext = str(item.get("fileExtension") or "").lower().lstrip(".")
    if not ext and "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
    dtype = _doc_type_guess(name, str(item.get("type") or role))
    return {
        "document_url": url,
        "document_name": name,
        "filename": name,
        "file_extension": ext or None,
        "document_type": dtype,
        "source_role": role,
        "attachment_id": item.get("id"),
        "shared_id": item.get("sharedId"),
        "appendix_id": item.get("appendixId"),
        "opengov_type": item.get("type"),
        "byte_size": item.get("size") or item.get("byteSize"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "free_public": True,
        "download_host": "s3" if "amazonaws.com" in url else "other",
    }


def extract_documents_from_project(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize attachments / snapshot / addenda / proposal docs from project payload."""
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def absorb(items: Any, role: str) -> None:
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, str) and item.startswith("http"):
                item = {"url": item, "filename": item.rsplit("/", 1)[-1]}
            if not isinstance(item, dict):
                continue
            # Nested file objects
            if not item.get("url") and isinstance(item.get("attachment"), dict):
                item = {**item.get("attachment"), **{k: v for k, v in item.items() if k != "attachment"}}
            if not item.get("url") and isinstance(item.get("file"), dict):
                item = {**item.get("file"), **{k: v for k, v in item.items() if k != "file"}}
            norm = _normalize_attachment(item, role=role)
            if not norm:
                continue
            key = norm["document_url"].split("?", 1)[0].lower()
            if key in seen:
                continue
            seen.add(key)
            docs.append(norm)

    absorb(payload.get("attachments"), "attachment")
    absorb(payload.get("documentAttachment"), "project_document_snapshot")
    absorb(payload.get("addendums") or payload.get("addenda"), "addendum")
    absorb(payload.get("proposalDocuments"), "proposal_document")
    absorb(payload.get("files"), "file")
    absorb(payload.get("documents"), "document")
    return docs


class OpenGovPublicDocumentClient:
    """Fetch public OpenGov project documents without authentication."""

    def __init__(self, *, timeout: float = 40.0, client: httpx.Client | None = None) -> None:
        self._owns = client is None
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        if self._owns:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self) -> "OpenGovPublicDocumentClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def fetch_project_documents(
        self,
        *,
        project_id: str | int,
        government_code: str | None = None,
    ) -> dict[str, Any]:
        """Return document metadata + access status for one project."""
        pid = str(project_id or "").strip()
        if not pid or not re.fullmatch(r"\d+", pid):
            return {
                "status": INVALID_PROJECT,
                "project_id": pid or None,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "error": "invalid_project_id",
                "retrieved_at": now_utc().isoformat(),
                "route": PROJECT_DETAIL_TMPL.format(project_id=pid or "{project_id}"),
            }

        url = PROJECT_DETAIL_TMPL.format(project_id=pid)
        try:
            r = self._client.get(url, headers=_headers(government_code, pid))
        except Exception as exc:
            return {
                "status": RETRYABLE_ERROR,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "error": type(exc).__name__,
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }

        if r.status_code in {401, 403}:
            return {
                "status": DOCUMENT_ACCESS_REQUIRES_AUTH,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "http_status": r.status_code,
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }
        if r.status_code == 404:
            return {
                "status": PROJECT_NOT_FOUND,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "http_status": 404,
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }
        if r.status_code >= 500 or r.status_code == 429:
            return {
                "status": RETRYABLE_ERROR,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "http_status": r.status_code,
                "error": f"http_{r.status_code}",
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }
        if not (200 <= r.status_code < 400):
            return {
                "status": RETRYABLE_ERROR,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "http_status": r.status_code,
                "error": f"http_{r.status_code}",
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }

        try:
            payload = r.json()
        except Exception:
            return {
                "status": RETRYABLE_ERROR,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "http_status": r.status_code,
                "error": "json_decode",
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }
        if not isinstance(payload, dict):
            return {
                "status": RETRYABLE_ERROR,
                "project_id": pid,
                "government_code": government_code,
                "documents": [],
                "document_count": 0,
                "error": "unexpected_payload",
                "retrieved_at": now_utc().isoformat(),
                "route": url,
            }

        gov = payload.get("government") if isinstance(payload.get("government"), dict) else {}
        code = government_code or gov.get("code")
        docs = extract_documents_from_project(payload)
        for d in docs:
            d["opengov_project_id"] = int(pid) if pid.isdigit() else pid
            d["opengov_government_code"] = code
            d["provenance"] = {
                "route": "GET /api/v1/project/{id}",
                "project_id": pid,
                "government_code": code,
                "source_url": f"https://procurement.opengov.com/portal/{code}/projects/{pid}"
                if code
                else url,
            }

        meta = {
            "title": payload.get("title"),
            "financial_id": payload.get("financialId"),
            "status": payload.get("status"),
            "proposal_deadline": payload.get("proposalDeadline"),
            "release_project_date": payload.get("releaseProjectDate"),
            "department": (payload.get("department") or {}).get("name")
            if isinstance(payload.get("department"), dict)
            else payload.get("departmentName"),
            "is_private": payload.get("isPrivate"),
            "requires_invitation": payload.get("requiresInvitation"),
            "government_code": code,
            "government_name": ((gov.get("organization") or {}) if isinstance(gov, dict) else {}).get(
                "name"
            ),
        }

        if docs:
            status = DOCUMENTS_FOUND_PUBLIC
        elif payload.get("isPrivate") or payload.get("requiresInvitation"):
            status = DOCUMENT_ACCESS_REQUIRES_AUTH
        else:
            status = NO_PUBLIC_DOCUMENTS

        return {
            "status": status,
            "project_id": pid,
            "government_code": code,
            "project_metadata": meta,
            "documents": docs,
            "document_count": len(docs),
            "type_counts": _type_counts(docs),
            "http_status": r.status_code,
            "retrieved_at": now_utc().isoformat(),
            "route": url,
            "content_hash": hashlib.sha1(r.content).hexdigest()[:16],
        }

    def download_bytes(self, url: str, *, max_bytes: int = 40_000_000) -> bytes | None:
        try:
            r = self._client.get(
                url,
                headers={"User-Agent": UA, "Accept": "*/*"},
                timeout=60.0,
            )
            if r.status_code >= 400 or not r.content:
                return None
            if len(r.content) > max_bytes:
                return r.content[:max_bytes]
            return r.content
        except Exception:
            return None


def _type_counts(docs: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in docs:
        t = str(d.get("document_type") or "attachment")
        out[t] = out.get(t, 0) + 1
        ext = str(d.get("file_extension") or "").lower()
        if ext:
            out[f"ext_{ext}"] = out.get(f"ext_{ext}", 0) + 1
    return out
