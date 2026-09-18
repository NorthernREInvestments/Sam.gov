"""Direct retrieval of already-stored public document URLs — zero SAM API usage."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from attachment_storage import persist_attachment_files
from models import Contract

_READINESS_PATTERNS: dict[str, re.Pattern[str]] = {
    "dell": re.compile(r"\bdell\b", re.I),
    "part_210_bnzh": re.compile(r"210[-_]?BNZH", re.I),
    "qty_14": re.compile(r"\b(qty\.?\s*14|quantity\s*[:\s]*14|fourteen\s+\w+)\b", re.I),
    "quantity_generic": re.compile(r"\b(qty\.?|quantity|ea\.?|each)\b", re.I),
    "clin": re.compile(r"\bCLIN\b", re.I),
    "specifications": re.compile(r"\b(specification|specs?|salient\s+characteristics)\b", re.I),
    "delivery_destination": re.compile(r"\b(place\s+of\s+performance|ship\s+to|delivery\s+location|destination)\b", re.I),
    "delivery_date": re.compile(r"\b(delivery\s+date|days?\s+ARO|period\s+of\s+performance)\b", re.I),
    "fob": re.compile(r"\bFOB\b", re.I),
    "shipping": re.compile(r"\b(shipping|freight|transportation)\b", re.I),
    "warranty": re.compile(r"\bwarrant(y|ies)\b", re.I),
    "brand_name_only": re.compile(r"brand[- ]name\s+only", re.I),
    "brand_name_or_equal": re.compile(r"brand[- ]name\s+or\s+equal|or\s+equal", re.I),
    "manufacturer_authorization": re.compile(r"\b(manufacturer\s+authorization|authorized\s+(reseller|dealer|partner))\b", re.I),
    "taa": re.compile(r"\b(TAA|Trade\s+Agreements?\s+Act)\b", re.I),
    "buy_american": re.compile(r"\bBuy\s+American\b", re.I),
    "country_of_origin": re.compile(r"\bcountry\s+of\s+origin\b", re.I),
    "nmr": re.compile(r"\b(NMR|non[- ]manufacturer\s+rule)\b", re.I),
    "set_aside": re.compile(r"\b(set[- ]aside|small\s+business)\b", re.I),
    "response_deadline": re.compile(r"\b(response\s+deadline|offers?\s+due|closing\s+date)\b", re.I),
    "submission_instructions": re.compile(r"\b(submit|proposal\s+submission|quotation\s+instructions)\b", re.I),
}


def collect_stored_public_urls(contract: Contract) -> list[dict[str, str]]:
    """URLs already stored on the opportunity — no network."""
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(url: str | None, kind: str) -> None:
        u = (url or "").strip()
        if not u or not u.startswith("http") or u in seen:
            return
        # noticedesc is a SAM API endpoint — do not treat as direct public document
        if "noticedesc" in u.lower():
            out.append({"url": u, "kind": "sam_noticedesc_api", "retrievable_direct": "no"})
            seen.add(u)
            return
        seen.add(u)
        out.append({"url": u, "kind": kind, "retrievable_direct": "yes"})

    for item in raw.get("resourceLinks") or []:
        if isinstance(item, str):
            _add(item, "resourceLinks")
        elif isinstance(item, dict):
            _add(item.get("url") or item.get("download_url") or item.get("href"), "resourceLinks")

    for att in raw.get("opportunityAttachments") or []:
        if isinstance(att, dict):
            _add(att.get("download_url") or att.get("url"), "opportunityAttachments")

    for u in raw.get("attachmentDownloadUrls") or []:
        if isinstance(u, str):
            _add(u, "attachmentDownloadUrls")

    desc = contract.description
    if isinstance(desc, str) and desc.strip().startswith("http"):
        _add(desc.strip(), "description_field")

    return out


def _with_api_key_if_sam_file(url: str) -> str:
    """Authenticate SAM file/description download URL without counting as SAM search API."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if "sam.gov" not in host:
        return url
    # File downloads OR official notice description endpoints
    needs_key = (
        "/resources/files/" in path
        or "/download" in path
        or "noticedesc" in path
        or "noticedesc" in (parsed.query or "").lower()
    )
    if not needs_key:
        return url
    key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not key:
        return url
    qs = parse_qs(parsed.query)
    if "api_key" in qs:
        return url
    qs["api_key"] = [key]
    return urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in qs.items()})))


def _filename_from_response(url: str, resp: httpx.Response, index: int) -> str:
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1).strip()
    path = urlparse(url).path.rstrip("/").split("/")[-1] or f"document_{index}"
    ctype = (resp.headers.get("content-type") or "").lower()
    if path.lower() == "download" or "." not in path:
        if "pdf" in ctype:
            return f"solicitation_{index}.pdf"
        if "html" in ctype:
            return f"solicitation_{index}.html"
        if "text" in ctype:
            return f"solicitation_{index}.txt"
        return f"solicitation_{index}.bin"
    return path


def retrieve_direct_public_documents(
    contract: Contract,
    session: Session,
    *,
    max_files: int = 12,
) -> dict[str, Any]:
    """
    HTTP GET already-stored public/resource URLs. Persist via existing attachment path.

    Does NOT call SAM search/enrich API. Does NOT count toward SAM API budget.
    """
    from pdf_text import extract_pdf_text

    stored = collect_stored_public_urls(contract)
    attempted: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    to_persist: list[tuple[str, bytes, str, str | None]] = []
    extracted_by_name: dict[str, str] = {}

    candidates = [u for u in stored if u.get("retrievable_direct") == "yes"][:max_files]
    skipped = [u for u in stored if u.get("retrievable_direct") != "yes"]

    for i, item in enumerate(candidates, 1):
        url = item["url"]
        fetch_url = _with_api_key_if_sam_file(url)
        entry: dict[str, Any] = {"url": url, "kind": item.get("kind")}
        try:
            with httpx.Client(timeout=180.0, follow_redirects=True) as client:
                resp = client.get(fetch_url)
            entry["http_status"] = resp.status_code
            if resp.status_code >= 400:
                entry["error"] = f"http_{resp.status_code}"
                attempted.append(entry)
                failed.append(entry)
                continue
            data = resp.content or b""
            filename = _filename_from_response(url, resp, i)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
            text = ""
            if data.startswith(b"%PDF"):
                text = extract_pdf_text(data) or ""
                ctype = ctype or "application/pdf"
            elif b"<html" in data[:2000].lower() or "text/html" in ctype:
                from sam_enrich import html_to_text

                text = html_to_text(data.decode("utf-8", errors="ignore"))
                ctype = ctype or "text/html"
            elif ctype.startswith("text/") or data[:1].isalpha():
                text = data.decode("utf-8", errors="ignore")
                ctype = ctype or "text/plain"

            entry.update(
                {
                    "filename": filename,
                    "content_type": ctype,
                    "file_size_bytes": len(data),
                    "extracted_text_chars": len(text),
                    "ok": True,
                }
            )
            attempted.append(entry)
            successful.append(entry)
            to_persist.append((filename, data, "direct_public_url", url))
            if text:
                extracted_by_name[filename] = text
        except Exception as exc:
            entry["error"] = str(exc)[:300]
            attempted.append(entry)
            failed.append(entry)

    written = 0
    if to_persist:
        written = persist_attachment_files(session, contract, to_persist, extracted_by_name=extracted_by_name)
        # Merge extracted text onto contract.attachment_text without SAM
        merged_parts = [extracted_by_name[k] for k in extracted_by_name if extracted_by_name[k]]
        if merged_parts:
            existing = (contract.attachment_text or "").strip()
            blob = "\n\n".join(merged_parts)
            contract.attachment_text = (existing + "\n\n" + blob).strip() if existing else blob
            contract.attachment_extraction_method = "direct_public_url"
            contract.attachment_extraction_note = (
                f"direct_public_url files={written} sam_api_calls=0"
            )
        session.flush()

    att_chars = len(contract.attachment_text or "")
    desc = contract.description or ""
    desc_chars = 0 if (isinstance(desc, str) and desc.startswith("http")) else len(desc)

    return {
        "opportunity_id": contract.id,
        "SAM_API_CALLS": 0,
        "OPENAI_CALLS": 0,
        "stored_urls": stored,
        "skipped_non_direct": skipped,
        "attempted": attempted,
        "successful": successful,
        "failed": failed,
        "documents_persisted": written,
        "attachment_text_chars": att_chars,
        "description_text_chars": desc_chars,
        "total_usable_corpus_chars": att_chars + desc_chars,
        "text_extraction_status": "ok" if att_chars else ("partial_or_empty" if successful else "failed"),
    }


def inspect_document_readiness(contract: Contract) -> dict[str, Any]:
    """Keyword presence inspection only — does NOT mark facts VERIFIED."""
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    parts = [
        contract.title or "",
        "" if (contract.description or "").startswith("http") else (contract.description or ""),
        contract.attachment_text or "",
        str(raw.get("solicitationNumber") or ""),
        str(raw.get("typeOfSetAsideDescription") or ""),
    ]
    corpus = "\n".join(parts)
    evidence: dict[str, Any] = {}
    for name, pattern in _READINESS_PATTERNS.items():
        m = pattern.search(corpus)
        evidence[name] = {
            "keyword_present": bool(m),
            "verification_status": "KEYWORD_ONLY_NOT_VERIFIED",
            "snippet": (m.group(0) if m else None),
        }

    major_missing = [
        k
        for k in (
            "specifications",
            "delivery_destination",
            "delivery_date",
            "submission_instructions",
            "qty_14",
        )
        if not evidence[k]["keyword_present"]
    ]
    return {
        "opportunity_id": getattr(contract, "id", None),
        "corpus_chars": len(corpus),
        "evidence": evidence,
        "major_missing_keyword_signals": major_missing,
        "note": "Keyword presence is readiness only — not VERIFIED procurement facts",
        "LIVE_API_REQUESTS": 0,
    }
