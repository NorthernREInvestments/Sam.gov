"""Complete solicitation package retrieval, authority model, and provenance."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import html as html_lib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from solicitation_package_constants import (
    ACCESS_AUTH_REQUIRED,
    ACCESS_BROKEN_LINK,
    ACCESS_LISTED_NOT_FETCHED,
    ACCESS_LOGIN_REQUIRED,
    ACCESS_NOT_FOUND,
    ACCESS_PUBLIC_FETCHED,
    ACCESS_UNKNOWN,
    ACCESS_UNSUPPORTED,
    DOC_AMENDMENT,
    DOC_BID_SCHEDULE,
    DOC_DELIVERY,
    DOC_DRAWING,
    DOC_LINE_ITEM_SCHEDULE,
    DOC_PRICING_SHEET,
    DOC_Q_AND_A,
    DOC_REFERENCE,
    DOC_SOLICITATION,
    DOC_SPECIFICATION,
    DOC_SOW,
    DOC_TERMS,
    DOC_UNKNOWN,
    DOC_VENDOR_FORM,
    SUPPORTED_EXTENSIONS,
)


def _utc() -> str:
    return now_utc().isoformat()


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify_document(
    *,
    title: str | None = None,
    filename: str | None = None,
    url: str | None = None,
    text_snippet: str | None = None,
) -> str:
    blob = f"{title or ''} {filename or ''} {url or ''} {(text_snippet or '')[:500]}".lower()
    if re.search(r"amend|addend", blob):
        return DOC_AMENDMENT
    if re.search(r"\bq\s*&?\s*a\b|question.?answer|questions?", blob) and "attachment" not in blob:
        if re.search(r"\bq\s*&?\s*a\b|addendum.*question", blob):
            return DOC_Q_AND_A
    if re.search(r"bid\s*schedule|line\s*item|pricing\s*sheet|price\s*schedule", blob):
        if "price" in blob or "pricing" in blob:
            return DOC_PRICING_SHEET
        return DOC_BID_SCHEDULE
    if re.search(r"pricing|price\s*sheet|bid\s*form", blob):
        return DOC_PRICING_SHEET
    if re.search(r"spec|specification|purchase\s+description", blob):
        return DOC_SPECIFICATION
    if re.search(r"statement\s+of\s+work|\bsow\b|scope\s+of\s+work", blob):
        return DOC_SOW
    if re.search(r"drawing|schematic|diagram", blob):
        return DOC_DRAWING
    if re.search(r"delivery|shipping\s+instruction", blob):
        return DOC_DELIVERY
    if re.search(r"terms\s+and\s+conditions|\bt&c\b|general\s+conditions", blob):
        return DOC_TERMS
    if re.search(r"vendor\s+form|w-9|registration|certification\s+form", blob):
        return DOC_VENDOR_FORM
    if re.search(r"event\.pdf|solicitation|rfp|rfq|rfb|ifb|invitation", blob):
        return DOC_SOLICITATION
    if re.search(r"reference|exhibit\s+[a-z]", blob):
        return DOC_REFERENCE
    if filename and Path(filename).suffix.lower() in {".xlsx", ".xls", ".csv"}:
        return DOC_LINE_ITEM_SCHEDULE
    return DOC_UNKNOWN


def file_type_from_name(name: str | None, content_type: str | None = None) -> str:
    if name:
        ext = Path(name).suffix.lower().lstrip(".")
        if ext:
            return ext.upper()
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return "PDF"
    if "html" in ct:
        return "HTML"
    if "csv" in ct:
        return "CSV"
    if "sheet" in ct or "excel" in ct:
        return "XLSX"
    if "word" in ct:
        return "DOCX"
    if "zip" in ct:
        return "ZIP"
    return "UNKNOWN"


def document_record(
    *,
    solicitation_number: str,
    source_portal: str,
    document_title: str | None,
    document_url: str | None,
    file_type: str | None = None,
    fetch_status: str = "ATTEMPTED",
    http_status: int | None = None,
    access_status: str = ACCESS_UNKNOWN,
    content_length: int | None = None,
    content_hash: str | None = None,
    provenance: str | None = None,
    extraction_status: str | None = None,
    extraction_error: str | None = None,
    appears_authoritative: bool | None = None,
    superseded_or_amended: bool = False,
    document_class: str | None = None,
    amendment_sequence: int | None = None,
    parent_archive: str | None = None,
    local_path: str | None = None,
) -> dict[str, Any]:
    title = document_title or (Path(urlparse(document_url or "").path).name if document_url else None)
    return {
        "solicitation_number": solicitation_number,
        "source_portal": source_portal,
        "document_title": title,
        "document_url": document_url,
        "file_type": file_type or file_type_from_name(title),
        "fetch_status": fetch_status,
        "http_status": http_status,
        "access_status": access_status,
        "content_length": content_length,
        "hash": content_hash,
        "provenance": provenance,
        "retrieval_timestamp": _utc(),
        "extraction_status": extraction_status,
        "extraction_error": extraction_error,
        "appears_authoritative": appears_authoritative,
        "superseded_or_amended": superseded_or_amended,
        "document_class": document_class
        or classify_document(title=title, filename=title, url=document_url),
        "amendment_sequence": amendment_sequence,
        "parent_archive": parent_archive,
        "local_path": local_path,
    }


def map_http_to_access(status: int | None, *, body: bytes | None = None, final_url: str | None = None) -> str:
    if status is None:
        return ACCESS_UNKNOWN
    if status == 200:
        text_head = (body or b"")[:2000].lower()
        final = (final_url or "").lower()
        if b"login" in text_head and b"password" in text_head:
            return ACCESS_LOGIN_REQUIRED
        if b"not a part of the jaggaer" in text_head or "supplierlogin" in final:
            return ACCESS_LOGIN_REQUIRED
        return ACCESS_PUBLIC_FETCHED
    if status in {401, 407}:
        return ACCESS_AUTH_REQUIRED
    if status == 403:
        return ACCESS_AUTH_REQUIRED
    if status == 404:
        return ACCESS_NOT_FOUND
    if status >= 400:
        return ACCESS_BROKEN_LINK
    return ACCESS_UNKNOWN


def resolve_document_authority(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Simple authority model:
    - later amendments beat earlier solicitation on conflicting facts
    - explicit line-item / bid schedule beats title inference
    - Q&A clarifies but does not auto-override formal amendments
    """
    ranked = sorted(
        documents,
        key=lambda d: (
            0 if d.get("document_class") == DOC_AMENDMENT else 1,
            -(d.get("amendment_sequence") or 0),
            0
            if d.get("document_class")
            in {DOC_LINE_ITEM_SCHEDULE, DOC_BID_SCHEDULE, DOC_PRICING_SHEET}
            else 1,
            0 if d.get("access_status") == ACCESS_PUBLIC_FETCHED else 1,
            d.get("retrieval_timestamp") or "",
        ),
    )
    for i, d in enumerate(ranked):
        d = dict(d)
        d["authority_rank"] = i
        d["appears_authoritative"] = d.get("access_status") == ACCESS_PUBLIC_FETCHED and d.get(
            "document_class"
        ) not in {DOC_REFERENCE, DOC_VENDOR_FORM, DOC_UNKNOWN}
        ranked[i] = d

    # Mark superseded originals when amendments exist
    has_amendment = any(d.get("document_class") == DOC_AMENDMENT for d in ranked)
    if has_amendment:
        for d in ranked:
            if d.get("document_class") == DOC_SOLICITATION:
                d["superseded_or_amended"] = True
                d["authority_note"] = "later_amendment_may_override"
    return ranked


def precedence_for_fact(fact_kind: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Pick winning fact among candidates with provenance.
    Each candidate: {value, document_class, amendment_sequence, explicit, source_document}
    """
    if not candidates:
        return None

    def score(c: dict[str, Any]) -> tuple:
        cls = c.get("document_class")
        return (
            0 if c.get("explicit") else 1,
            0 if cls == DOC_AMENDMENT else 1,
            -(c.get("amendment_sequence") or 0),
            0
            if cls in {DOC_LINE_ITEM_SCHEDULE, DOC_BID_SCHEDULE, DOC_PRICING_SHEET}
            and fact_kind in {"quantity", "line_item", "uom", "price"}
            else 1,
            1 if cls == DOC_Q_AND_A else 0,  # Q&A lower unless only source
            0 if c.get("value") not in (None, "", "UNKNOWN") else 1,
        )

    # Q&A alone should not beat amendment; filter if amendments present
    has_amend = any(c.get("document_class") == DOC_AMENDMENT for c in candidates)
    pool = candidates
    if has_amend and fact_kind != "clarification":
        non_qa = [c for c in candidates if c.get("document_class") != DOC_Q_AND_A]
        if non_qa:
            pool = non_qa
    return sorted(pool, key=score)[0]


def enumerate_sciquest_package_from_listing_row(
    row_html: str,
    *,
    solicitation_number: str,
    list_url: str,
) -> list[dict[str, Any]]:
    """Enumerate package docs discoverable from a SciQuest listing row."""
    docs: list[dict[str, Any]] = []
    hrefs = [html_lib.unescape(h) for h in re.findall(r'href="([^"]+)"', row_html)]
    for h in hrefs:
        if "Sourcingevent/" in h and "event.pdf" in h:
            docs.append(
                document_record(
                    solicitation_number=solicitation_number,
                    source_portal="sciquest_iowa",
                    document_title=f"{solicitation_number}-event.pdf",
                    document_url=h,
                    file_type="PDF",
                    fetch_status="DISCOVERED",
                    access_status=ACCESS_LISTED_NOT_FETCHED,
                    provenance=f"listing_row:{list_url}",
                    document_class=DOC_SOLICITATION,
                    appears_authoritative=True,
                )
            )
        elif "ViewSourcingEvent" in h:
            docs.append(
                document_record(
                    solicitation_number=solicitation_number,
                    source_portal="sciquest_iowa",
                    document_title="ViewSourcingEvent detail",
                    document_url=h,
                    file_type="HTML",
                    fetch_status="DISCOVERED",
                    access_status=ACCESS_LISTED_NOT_FETCHED,
                    provenance=f"listing_row:{list_url}",
                    document_class=DOC_SOLICITATION,
                )
            )
    return docs


def enumerate_listed_attachments_from_text(
    text: str,
    *,
    solicitation_number: str,
    source_portal: str,
    parent_document: str | None = None,
) -> list[dict[str, Any]]:
    """Buyer Attachments named in event PDF but without public URL → listed not fetched / likely auth."""
    docs: list[dict[str, Any]] = []
    am = re.search(
        r"Buyer Attachments\s*(.*?)(?:Questions|Product Line Items|Required to View|\Z)",
        text or "",
        re.I | re.S,
    )
    if not am:
        return docs
    for lm in re.finditer(r"\d+\.\s*\n?\s*([^\n]+\.(?:pdf|docx?|xlsx?|csv|zip|txt))", am.group(1), re.I):
        name = re.sub(r"\s+", " ", lm.group(1)).strip()
        docs.append(
            document_record(
                solicitation_number=solicitation_number,
                source_portal=source_portal,
                document_title=name,
                document_url=None,
                file_type=file_type_from_name(name),
                fetch_status="LISTED_NO_PUBLIC_URL",
                access_status=ACCESS_LOGIN_REQUIRED,
                provenance=f"named_in:{parent_document or 'event_pdf'}",
                document_class=classify_document(title=name, filename=name),
                appears_authoritative=True,
                extraction_status="NOT_ATTEMPTED",
                extraction_error="attachment_requires_portal_login_no_public_url",
            )
        )
    return docs


def fetch_document(
    url: str,
    *,
    client: Any,
    solicitation_number: str,
    source_portal: str,
    title: str | None = None,
    cache_dir: Path | None = None,
    document_class: str | None = None,
) -> dict[str, Any]:
    """Fetch one document via PublicProcurementHttpClient-like client (.get -> HttpResponse)."""
    ext = Path(urlparse(url).path).suffix.lower()
    if ext and ext not in SUPPORTED_EXTENSIONS and "event.pdf" not in url:
        rec = document_record(
            solicitation_number=solicitation_number,
            source_portal=source_portal,
            document_title=title,
            document_url=url,
            fetch_status="SKIPPED",
            access_status=ACCESS_UNSUPPORTED,
            provenance="fetch_document",
            document_class=document_class,
        )
        return {**rec, "content": None, "text": None}

    try:
        resp = client.get(url, source_id=source_portal)
    except Exception as exc:
        return document_record(
            solicitation_number=solicitation_number,
            source_portal=source_portal,
            document_title=title,
            document_url=url,
            fetch_status="ERROR",
            access_status=ACCESS_UNKNOWN,
            provenance="fetch_document",
            extraction_error=str(exc)[:300],
            document_class=document_class,
        )

    status = getattr(resp, "status_code", None)
    content = getattr(resp, "content", b"") or b""
    final_url = None
    meta = getattr(resp, "meta", None)
    if meta is not None:
        final_url = getattr(meta, "url", None)
    access = map_http_to_access(status, body=content, final_url=final_url)
    h = content_sha256(content) if content and access == ACCESS_PUBLIC_FETCHED else None
    local_path = None
    text = None
    extraction_status = None
    extraction_error = None

    if access == ACCESS_PUBLIC_FETCHED and content:
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            fname = title or Path(urlparse(url).path).name or "document.bin"
            # strip query-ish names
            fname = re.sub(r"[^\w.\-]+", "_", fname)[:120]
            path = cache_dir / f"{solicitation_number}__{fname}"
            path.write_bytes(content)
            local_path = str(path)
        if content.startswith(b"%PDF"):
            try:
                from pdf_text import extract_pdf_text

                text = extract_pdf_text(content) or ""
                extraction_status = "TEXT_EXTRACTED" if text else "EMPTY_TEXT"
            except Exception as exc:
                extraction_status = "FAILED"
                extraction_error = str(exc)[:200]
        elif b"<html" in content[:2000].lower() or (title or "").endswith((".html", ".htm")):
            text = content.decode("utf-8", errors="replace")
            extraction_status = "HTML_LOADED"
        else:
            extraction_status = "BINARY_STORED"

    headers = getattr(resp, "headers", None) or {}
    ctype = None
    if isinstance(headers, dict):
        ctype = headers.get("content-type") or headers.get("Content-Type")
    rec = document_record(
        solicitation_number=solicitation_number,
        source_portal=source_portal,
        document_title=title,
        document_url=url.split("?")[0] if url else url,
        file_type=file_type_from_name(title, ctype),
        fetch_status="FETCHED" if access == ACCESS_PUBLIC_FETCHED else "FAILED",
        http_status=status,
        access_status=access,
        content_length=len(content) if content else 0,
        content_hash=h,
        provenance="fetch_document",
        extraction_status=extraction_status,
        extraction_error=extraction_error,
        appears_authoritative=access == ACCESS_PUBLIC_FETCHED,
        document_class=document_class or classify_document(title=title, url=url, text_snippet=text),
        local_path=local_path,
    )
    # Preserve signed URL separately for audit
    rec["fetch_url_had_signature"] = bool(url and "X-Amz" in url)
    rec["content"] = content if access == ACCESS_PUBLIC_FETCHED else None
    rec["text"] = text
    return rec
