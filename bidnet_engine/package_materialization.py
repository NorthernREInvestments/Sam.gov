"""BidNet package materialization — truthful package states + local document acquisition.

PACKAGE_ACQUIRED_OFFICIAL_SOURCE previously meant URL/detail/page evidence, not local files.
This module materializes attachments, validates content, and gates line-extraction readiness.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20261007-m3-bidnet-package-materialization-v1"

# Truthful package stages (production)
PACKAGE_SOURCE_IDENTIFIED = "PACKAGE_SOURCE_IDENTIFIED"
PACKAGE_DETAIL_ACQUIRED = "PACKAGE_DETAIL_ACQUIRED"
PACKAGE_ATTACHMENT_INDEX_ACQUIRED = "PACKAGE_ATTACHMENT_INDEX_ACQUIRED"
PACKAGE_DOCUMENTS_PARTIAL = "PACKAGE_DOCUMENTS_PARTIAL"
PACKAGE_DOCUMENTS_COMPLETE = "PACKAGE_DOCUMENTS_COMPLETE"
PACKAGE_DOCUMENTS_UNAVAILABLE = "PACKAGE_DOCUMENTS_UNAVAILABLE"
PACKAGE_REGISTRATION_REQUIRED = "PACKAGE_REGISTRATION_REQUIRED"
PACKAGE_MEMBERSHIP_LOCKED = "PACKAGE_MEMBERSHIP_LOCKED"
PACKAGE_EXTERNAL_PORTAL_REQUIRED = "PACKAGE_EXTERNAL_PORTAL_REQUIRED"
PACKAGE_DOWNLOAD_FAILED = "PACKAGE_DOWNLOAD_FAILED"
PACKAGE_UNKNOWN = "PACKAGE_UNKNOWN"

# Legacy aliases kept for compatibility
LEGACY_ACQUIRED = {
    "PACKAGE_ACQUIRED_OFFICIAL_SOURCE",
    "PACKAGE_ACQUIRED_BIDNET",
    "PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE",
    "PACKAGE_ACQUIRED_CACHED",
    "PACKAGE_ACQUIRED",
}

COMPLETENESS = ("NONE", "DETAIL_ONLY", "PARTIAL", "LIKELY_COMPLETE", "COMPLETE", "UNKNOWN")

_HIGH_VALUE = re.compile(
    r"(pric(?:e|ing)|bid\s*form|bid\s*schedule|item\s*list|line[\-\s]?item|"
    r"quote|proposal\s*form|cost\s*sheet|material(?:s)?\s*list|equipment\s*list|"
    r"supply\s*list|\bbom\b|bill\s*of\s*materials|schedule|spreadsheet|"
    r"excel|xlsx?|csv|exhibit|appendix|attachment)",
    re.I,
)
_LOGIN_HTML = re.compile(
    r"(please\s*log\s*in|sign\s*in|cloudflare|access\s*denied|captcha|"
    r"supplier\s*registration|just\s*a\s*moment|attention\s*required)",
    re.I,
)
_GENERIC_NAME = re.compile(
    r"^(attachment|exhibit|appendix|document|file|doc|addendum|amendment)\s*[a-z0-9._-]*$",
    re.I,
)


def _sig_ok(data: bytes, ext: str) -> tuple[bool, str | None]:
    if not data or len(data) < 8:
        return False, "empty_or_tiny"
    head = data[:16]
    ext = (ext or "").lower().lstrip(".")
    # HTML masquerading
    text_head = data[:400].decode("utf-8", errors="ignore")
    if _LOGIN_HTML.search(text_head) or text_head.lstrip().lower().startswith("<!doctype html") or text_head.lstrip().lower().startswith("<html"):
        if ext in {"pdf", "xlsx", "xls", "docx", "doc", "csv"}:
            return False, "html_or_login_page_saved_as_binary"
    if ext == "pdf":
        if data[:5] == b"%PDF-":
            return True, None
        return False, "not_pdf_signature"
    if ext in {"xlsx", "docx"}:
        try:
            if not zipfile.is_zipfile(BytesIO(data)):
                return False, "not_openxml_zip"
            with zipfile.ZipFile(BytesIO(data)) as zf:
                names = zf.namelist()
            if ext == "xlsx" and not any(n.startswith("xl/") for n in names):
                return False, "zip_not_xlsx_workbook"
            if ext == "docx" and not any(n.startswith("word/") for n in names):
                return False, "zip_not_docx"
            return True, None
        except Exception as exc:
            return False, f"openxml_invalid:{type(exc).__name__}"
    if ext == "xls":
        if head[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
            return True, None
        return False, "not_ole_xls"
    if ext in {"csv", "txt"}:
        try:
            sample = data[:4000].decode("utf-8", errors="replace")
        except Exception:
            return False, "not_text"
        if _LOGIN_HTML.search(sample):
            return False, "html_login_as_text"
        if sample.count(",") + sample.count("\t") + sample.count("\n") < 2:
            return False, "not_parseable_tabular_text"
        return True, None
    if ext in {"html", "htm"}:
        if _LOGIN_HTML.search(text_head):
            return False, "auth_or_error_html"
        return True, None
    # Unknown extension — accept non-empty non-login blobs
    if _LOGIN_HTML.search(text_head):
        return False, "login_or_challenge_content"
    return True, None


def validate_local_file(path: str | Path, *, claimed_ext: str | None = None) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"DOCUMENT_CONTENT_VALID": False, "reason": "local_file_missing", "byte_size": 0}
    try:
        data = p.read_bytes()
    except Exception as exc:
        return {"DOCUMENT_CONTENT_VALID": False, "reason": f"read_error:{type(exc).__name__}", "byte_size": 0}
    ext = (claimed_ext or p.suffix or "").lower().lstrip(".")
    ok, reason = _sig_ok(data, ext)
    return {
        "DOCUMENT_CONTENT_VALID": ok,
        "reason": reason,
        "byte_size": len(data),
        "content_hash": hashlib.sha256(data).hexdigest()[:16] if data else None,
        "mime_guess": ext or None,
    }


def role_guess(name: str, url: str = "") -> str:
    blob = f"{name} {url}".lower()
    if "amend" in blob or "addendum" in blob:
        return "AMENDMENT"
    if re.search(r"\b(bom|bill\s*of\s*materials|material\s*list)\b", blob):
        return "BOM"
    if re.search(r"\b(bid\s*form|itemized\s*bid|proposal\s*form)\b", blob):
        return "BID_FORM"
    if re.search(r"\b(pric(?:e|ing)|cost\s*sheet|unit\s*price|xlsx|xls|csv)\b", blob):
        return "PRICING_SCHEDULE"
    if re.search(r"\b(line[\-\s]?item|item\s*list|equipment\s*list|supply\s*list|bid\s*schedule)\b", blob):
        return "LINE_ITEM_SCHEDULE"
    if re.search(r"\b(spec|scope|technical)\b", blob):
        return "SPECIFICATION"
    if _GENERIC_NAME.match((name or "").strip()) or re.search(r"\b(attachment|exhibit|appendix)\b", blob):
        return "GENERIC_ATTACHMENT"
    return "ATTACHMENT"


def build_attachment_index(docs: list[dict[str, Any]], *, opportunity_id: str = "", source_system: str = "BidNet") -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for i, d in enumerate(docs or []):
        if not isinstance(d, dict):
            continue
        name = str(d.get("document_name") or d.get("filename") or d.get("name") or f"document_{i+1}")
        url = str(d.get("document_url") or d.get("url") or d.get("source_url") or "")
        ext = Path(name).suffix.lower().lstrip(".") or Path(url.split("?")[0]).suffix.lower().lstrip(".")
        role = d.get("document_role") or role_guess(name, url)
        index.append(
            {
                "document_id": d.get("document_id") or d.get("content_hash") or f"doc-{i+1}-{hashlib.md5(name.encode()).hexdigest()[:8]}",
                "display_name": name,
                "filename": name,
                "extension": ext,
                "source_url": url or None,
                "source_system": d.get("source_system") or source_system,
                "official_source": bool(d.get("official_source") or "bidnet" not in url.lower()) if url else False,
                "requires_auth": bool(d.get("requires_auth")),
                "requires_registration": bool(d.get("requires_registration")),
                "downloadable": bool(url.startswith("http") or d.get("local_path")),
                "document_role_guess": role,
                "amendment_number": d.get("amendment_number"),
                "file_size": d.get("size_bytes") or d.get("file_size"),
                "mime_type": d.get("mime_type"),
                "discovered_at": d.get("discovered_at") or d.get("retrieved_at") or now_utc().isoformat(),
                "local_path": d.get("local_path"),
                "retrieval_status": d.get("retrieval_status"),
                "high_value": bool(_HIGH_VALUE.search(f"{name} {url}") or role in {
                    "PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM", "GENERIC_ATTACHMENT"
                }),
                "_raw": d,
            }
        )
    # Rank high-value first; generic attachments still kept (do not ignore Exhibit A)
    index.sort(key=lambda x: (0 if x.get("high_value") else 1, str(x.get("filename") or "")))
    return index


def classify_access_blocker(doc: dict[str, Any], *, detail_status: str | None = None, error: str | None = None) -> str:
    blob = f"{doc.get('source_url') or ''} {doc.get('retrieval_status') or ''} {error or ''} {detail_status or ''}".lower()
    if "registration" in blob or detail_status == "SUPPLIER_REGISTRATION_REDIRECT":
        return "FREE_REGISTRATION_REQUIRED"
    if "member" in blob or "paid" in blob or "subscription" in blob:
        return "PAID_MEMBERSHIP_REQUIRED"
    if "captcha" in blob or "cloudflare" in blob or "challenge" in blob:
        return "CAPTCHA_OR_BOT_CHALLENGE"
    if "auth" in blob or "login" in blob or "session" in blob:
        return "AUTHENTICATED_EXISTING_ACCOUNT"
    if "404" in blob or "removed" in blob or "not found" in blob:
        return "DOCUMENT_REMOVED"
    if "download_failed" in blob or "DOWNLOAD_FAILED" in str(doc.get("retrieval_status") or ""):
        return "DOWNLOAD_ERROR"
    if doc.get("source_url") and "bidnet" not in str(doc.get("source_url")).lower():
        return "EXTERNAL_PORTAL_UNKNOWN"
    if doc.get("local_path") or doc.get("retrieval_status") == "DOWNLOADED":
        return "FREE_PUBLIC"
    return "OTHER"


def materialize_attachments(
    docs: list[dict[str, Any]],
    *,
    opportunity_id: str,
    client: Any | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Download missing attachments, validate content, return materialized index."""
    from m3_data_root import data_path

    index = build_attachment_index(docs, opportunity_id=opportunity_id)
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]+", "_", opportunity_id or "unknown")[:48]
    downloaded = 0
    valid = 0
    invalid: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []

    # Prefer high-value first
    for entry in index[:limit]:
        raw = entry.get("_raw") if isinstance(entry.get("_raw"), dict) else {}
        path_s = entry.get("local_path") or raw.get("local_path")
        # Re-validate existing
        if path_s and Path(path_s).exists():
            v = validate_local_file(path_s, claimed_ext=entry.get("extension"))
            entry["LOCAL_PATH"] = path_s
            entry["CONTENT_HASH"] = v.get("content_hash") or raw.get("content_hash")
            entry["BYTE_SIZE"] = v.get("byte_size")
            entry["DOCUMENT_CONTENT_VALID"] = v.get("DOCUMENT_CONTENT_VALID")
            entry["DOCUMENT_VALIDATION_FAILURE_REASON"] = v.get("reason")
            entry["DOWNLOAD_TIME"] = raw.get("retrieved_at")
            entry["SOURCE_URL"] = entry.get("source_url")
            entry["SOURCE_DOCUMENT_ID"] = entry.get("document_id")
            if v.get("DOCUMENT_CONTENT_VALID"):
                valid += 1
                downloaded += 1
                materialized.append(entry)
            else:
                invalid.append({**entry, "failure": v.get("reason")})
                # try re-download if URL available
                path_s = None
            if path_s:
                continue

        url = str(entry.get("source_url") or "")
        if not url.startswith("http") or client is None or not hasattr(client, "download_bytes"):
            entry["access_class"] = classify_access_blocker(entry)
            if entry.get("downloadable"):
                entry["blocker"] = "SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE" if entry.get("high_value") else "DOWNLOAD_FAILED"
            continue
        try:
            body = client.download_bytes(url, timeout_ms=60_000)
        except Exception as exc:
            entry["retrieval_status"] = "DOWNLOAD_FAILED"
            entry["access_class"] = classify_access_blocker(entry, error=str(exc))
            entry["blocker"] = "DOWNLOAD_FAILED"
            entry["error"] = f"{type(exc).__name__}:{exc}"[:200]
            continue
        name = entry.get("filename") or "doc"
        ext = entry.get("extension") or Path(name).suffix.lower().lstrip(".")
        v = _sig_ok(body or b"", ext)
        h = hashlib.sha256(body or b"").hexdigest()[:16]
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(name))[:80]
        path = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if body:
            path.write_bytes(body)
        entry["LOCAL_PATH"] = str(path)
        entry["CONTENT_HASH"] = h
        entry["BYTE_SIZE"] = len(body or b"")
        entry["DOWNLOAD_TIME"] = now_utc().isoformat()
        entry["SOURCE_URL"] = url
        entry["SOURCE_DOCUMENT_ID"] = entry.get("document_id")
        entry["MIME_TYPE"] = entry.get("mime_type")
        entry["DOCUMENT_CONTENT_VALID"] = v[0]
        entry["DOCUMENT_VALIDATION_FAILURE_REASON"] = v[1]
        entry["retrieval_status"] = "DOWNLOADED" if v[0] else "INVALID_CONTENT"
        # Mirror onto raw doc for downstream extractors
        raw["local_path"] = str(path)
        raw["content_hash"] = h
        raw["retrieval_status"] = entry["retrieval_status"]
        raw["retrieved_at"] = entry["DOWNLOAD_TIME"]
        raw["size_bytes"] = entry["BYTE_SIZE"]
        downloaded += 1
        if v[0]:
            valid += 1
            materialized.append(entry)
        else:
            invalid.append({**entry, "failure": v[1]})
            try:
                path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass

    # Amendment graph (filename heuristics)
    amendments = [e for e in index if e.get("document_role_guess") == "AMENDMENT" or "addend" in str(e.get("filename") or "").lower()]
    pricing = [e for e in materialized if e.get("document_role_guess") in {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM"}]
    for e in pricing:
        e["CURRENT_AUTHORITATIVE"] = True
        e["SUPERSEDES"] = None
        e["SUPERSEDED_BY"] = None
    if len(pricing) >= 2:
        # Prefer names with revised/addendum/final
        def _rank(e: dict[str, Any]) -> int:
            n = str(e.get("filename") or "").lower()
            score = 0
            if "revis" in n or "final" in n or "updated" in n:
                score += 3
            if "addend" in n or "amend" in n:
                score += 2
            if e.get("extension") in {"xlsx", "xls", "csv"}:
                score += 2
            return score

        pricing_sorted = sorted(pricing, key=_rank, reverse=True)
        for e in pricing_sorted[1:]:
            e["CURRENT_AUTHORITATIVE"] = False
            e["SUPERSEDED_BY"] = pricing_sorted[0].get("document_id")
        pricing_sorted[0]["SUPERSEDES"] = [e.get("document_id") for e in pricing_sorted[1:]]
        pricing_sorted[0]["CURRENT_AUTHORITATIVE"] = True

    auth_doc = None
    for e in materialized:
        if e.get("CURRENT_AUTHORITATIVE") and e.get("document_role_guess") in {
            "PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM"
        }:
            auth_doc = e
            break
    if not auth_doc:
        for e in materialized:
            if e.get("high_value") or e.get("document_role_guess") == "GENERIC_ATTACHMENT":
                # Generic Attachment/Exhibit may still hold the schedule
                if e.get("extension") in {"xlsx", "xls", "csv", "docx", "pdf"}:
                    auth_doc = e
                    break
    if not auth_doc and materialized:
        # Prefer any spreadsheet
        for e in materialized:
            if e.get("extension") in {"xlsx", "xls", "csv"}:
                auth_doc = e
                break

    discovered = len(index)
    expected = discovered  # without site-declared count, expected ≈ discovered
    completeness, package_state, ready, blocker = reconcile_package_state(
        discovered=discovered,
        downloaded=downloaded,
        valid=valid,
        auth_doc=auth_doc,
        index=index,
        detail_present=True,
    )

    return {
        "build": BUILD,
        "opportunity_id": opportunity_id,
        "PACKAGE_ATTACHMENT_INDEX": [{k: v for k, v in e.items() if k != "_raw"} for e in index],
        "materialized": [{k: v for k, v in e.items() if k != "_raw"} for e in materialized],
        "invalid_downloads": [{k: v for k, v in e.items() if k != "_raw"} for e in invalid],
        "PACKAGE_DOCUMENT_COUNT_EXPECTED": expected,
        "PACKAGE_DOCUMENT_COUNT_DISCOVERED": discovered,
        "PACKAGE_DOCUMENT_COUNT_DOWNLOADED": downloaded,
        "PACKAGE_DOCUMENT_COUNT_MATERIALIZED": valid,
        "PACKAGE_COMPLETENESS": completeness,
        "package_state_truthful": package_state,
        "PACKAGE_READY_FOR_LINE_EXTRACTION": ready,
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": bool(auth_doc),
        "AUTHORITATIVE_PRODUCT_DOC_ID": (auth_doc or {}).get("document_id"),
        "AUTHORITATIVE_PRODUCT_DOC_ROLE": (auth_doc or {}).get("document_role_guess"),
        "AUTHORITATIVE_PRODUCT_DOC": {k: v for k, v in (auth_doc or {}).items() if k != "_raw"} if auth_doc else None,
        "amendments_count": len(amendments),
        "primary_blocker": blocker,
        "docs_for_extraction": [
            {
                **(e.get("_raw") or {}),
                "local_path": e.get("LOCAL_PATH") or e.get("local_path"),
                "filename": e.get("filename"),
                "document_name": e.get("filename"),
                "document_url": e.get("source_url"),
                "document_type": "pricing_sheet"
                if e.get("document_role_guess") in {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE"}
                else e.get("document_role_guess"),
                "content_hash": e.get("CONTENT_HASH"),
                "retrieval_status": e.get("retrieval_status") or "DOWNLOADED",
            }
            for e in materialized
        ],
        "operator_message": operator_package_message(
            completeness=completeness,
            ready=ready,
            auth_doc=auth_doc,
            discovered=discovered,
            valid=valid,
            blocker=blocker,
            missing_high_value=_missing_high_value(index, materialized),
        ),
        "updated_at": now_utc().isoformat(),
    }


def _missing_high_value(index: list[dict[str, Any]], materialized: list[dict[str, Any]]) -> list[str]:
    have = {e.get("document_id") for e in materialized}
    missing = []
    for e in index:
        if e.get("high_value") and e.get("document_id") not in have:
            if e.get("document_role_guess") in {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM"}:
                missing.append(str(e.get("filename") or e.get("document_id")))
    return missing


def reconcile_package_state(
    *,
    discovered: int,
    downloaded: int,
    valid: int,
    auth_doc: dict[str, Any] | None,
    index: list[dict[str, Any]],
    detail_present: bool,
) -> tuple[str, str, bool, str | None]:
    """Return completeness, package_state, ready_for_line_extraction, primary_blocker."""
    if discovered == 0:
        if detail_present:
            return "DETAIL_ONLY", PACKAGE_DETAIL_ACQUIRED, False, "NO_ATTACHMENT_INDEX"
        return "NONE", PACKAGE_SOURCE_IDENTIFIED, False, "NO_ATTACHMENT_INDEX"

    missing_pricing = any(
        e.get("document_role_guess") in {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM"}
        and not (e.get("LOCAL_PATH") or e.get("local_path"))
        for e in index
    )
    # Access classification from index
    access = [classify_access_blocker(e) for e in index if not (e.get("LOCAL_PATH") or e.get("local_path"))]
    if valid == 0 and any(a == "FREE_REGISTRATION_REQUIRED" for a in access):
        return "NONE", PACKAGE_REGISTRATION_REQUIRED, False, "REGISTRATION_REQUIRED"
    if valid == 0 and any(a == "PAID_MEMBERSHIP_REQUIRED" for a in access):
        return "NONE", PACKAGE_MEMBERSHIP_LOCKED, False, "PAID_MEMBERSHIP_REQUIRED"
    if valid == 0 and any(a == "CAPTCHA_OR_BOT_CHALLENGE" for a in access):
        return "NONE", PACKAGE_DOWNLOAD_FAILED, False, "BOT_CHALLENGE"
    if valid == 0 and any(a in {"EXTERNAL_PORTAL_UNKNOWN"} for a in access) and downloaded == 0:
        return "DETAIL_ONLY", PACKAGE_EXTERNAL_PORTAL_REQUIRED, False, "EXTERNAL_PORTAL_NOT_RESOLVED"
    if valid == 0 and discovered > 0:
        return "PARTIAL", PACKAGE_DOWNLOAD_FAILED if downloaded == 0 else PACKAGE_DOCUMENTS_PARTIAL, False, (
            "INVALID_DOWNLOADED_FILE" if downloaded > 0 else "DOWNLOAD_FAILED"
        )

    ready = bool(auth_doc and auth_doc.get("DOCUMENT_CONTENT_VALID", True) and (
        auth_doc.get("LOCAL_PATH") or auth_doc.get("local_path")
    ))
    # Must have local authoritative product doc; no unresolved known pricing attachment
    if ready and missing_pricing:
        # If we have SOME pricing materialized as auth_doc, ignore other missing generics
        if auth_doc and auth_doc.get("document_role_guess") in {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM"}:
            missing_pricing = False
        else:
            ready = False

    if valid >= discovered and discovered > 0 and ready:
        return "COMPLETE", PACKAGE_DOCUMENTS_COMPLETE, True, None
    if ready and valid >= max(1, int(0.7 * discovered)):
        return "LIKELY_COMPLETE", PACKAGE_DOCUMENTS_COMPLETE, True, None
    if valid > 0:
        blocker = "PRODUCT_SCHEDULE_NOT_ACQUIRED" if not auth_doc else "SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE"
        if not auth_doc:
            # Distinguish: no schedule in package vs not acquired
            high = [e for e in index if e.get("high_value")]
            if not high:
                blocker = "SCHEDULE_NOT_PRESENT"
            else:
                blocker = "PRODUCT_SCHEDULE_NOT_ACQUIRED"
        return "PARTIAL", PACKAGE_DOCUMENTS_PARTIAL, ready, blocker if not ready else None
    return "DETAIL_ONLY", PACKAGE_ATTACHMENT_INDEX_ACQUIRED, False, "SCHEDULE_NOT_DISCOVERED"


def operator_package_message(
    *,
    completeness: str,
    ready: bool,
    auth_doc: dict[str, Any] | None,
    discovered: int,
    valid: int,
    blocker: str | None,
    missing_high_value: list[str] | None = None,
) -> str:
    if ready and auth_doc:
        name = auth_doc.get("filename") or "product schedule"
        return f"Pricing/product document found ({name}) — {valid} of {discovered} documents acquired."
    if blocker == "REGISTRATION_REQUIRED":
        return "Product schedule is on the buyer portal and requires free registration."
    if blocker == "PAID_MEMBERSHIP_REQUIRED":
        return "Documents require paid membership — not downloadable with current access."
    if blocker == "BOT_CHALLENGE":
        return "Download blocked by a bot/CAPTCHA challenge."
    if blocker == "EXTERNAL_PORTAL_NOT_RESOLVED":
        return "Package appears to live on an external buyer portal that is not resolved yet."
    if blocker == "SCHEDULE_NOT_PRESENT":
        return "Package contains no itemized product schedule."
    if blocker == "PRODUCT_SCHEDULE_NOT_ACQUIRED" or (missing_high_value and not auth_doc):
        miss = (missing_high_value or ["pricing attachment"])[0]
        return f"We found evidence of a pricing schedule, but do not have the file yet ({miss})."
    if completeness == "PARTIAL" and missing_high_value:
        return f"Package incomplete — {len(missing_high_value)} required pricing attachment(s) missing."
    if completeness == "DETAIL_ONLY":
        return "Detail page acquired, but no package attachments were found yet."
    if blocker == "INVALID_DOWNLOADED_FILE":
        return "Downloaded files were invalid (login/error page or corrupt) and were rejected."
    if blocker == "DOWNLOAD_FAILED":
        return "Attachment links were found but downloads failed."
    return f"Package status: {completeness.lower().replace('_', ' ')} — {valid} of {discovered} documents acquired."


def primary_line_blocker(
    *,
    package_result: dict[str, Any],
    lines_ready: bool,
    schedule_extract: dict[str, Any] | None = None,
) -> str:
    """Distinguish PRODUCT_SCHEDULE_NOT_ACQUIRED from PARSER_FAILURE."""
    if package_result.get("PACKAGE_READY_FOR_LINE_EXTRACTION") and not lines_ready:
        zeros = (schedule_extract or {}).get("SCHEDULE_PRESENT_EXTRACTION_ZERO") or []
        if zeros:
            return "PARSER_FAILURE"
        return "PARSER_FAILURE"
    b = package_result.get("primary_blocker")
    if b == "PRODUCT_SCHEDULE_NOT_ACQUIRED":
        return "PRODUCT_SCHEDULE_NOT_ACQUIRED"
    mapping = {
        "NO_ATTACHMENT_INDEX": "NO_ATTACHMENT_INDEX",
        "SCHEDULE_NOT_DISCOVERED": "SCHEDULE_NOT_DISCOVERED",
        "SCHEDULE_NOT_PRESENT": "SCHEDULE_NOT_PRESENT",
        "REGISTRATION_REQUIRED": "REGISTRATION_REQUIRED",
        "PAID_MEMBERSHIP_REQUIRED": "PAID_MEMBERSHIP_REQUIRED",
        "BOT_CHALLENGE": "BOT_CHALLENGE",
        "EXTERNAL_PORTAL_NOT_RESOLVED": "EXTERNAL_PORTAL_NOT_RESOLVED",
        "DOWNLOAD_FAILED": "DOWNLOAD_FAILED",
        "INVALID_DOWNLOADED_FILE": "INVALID_DOWNLOADED_FILE",
        "SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE": "SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE",
    }
    return mapping.get(str(b), str(b or "OTHER"))


def legacy_package_implies_complete(state: str) -> bool:
    """True if old semantics treated this as 'package ready' without local files."""
    return state in LEGACY_ACQUIRED
