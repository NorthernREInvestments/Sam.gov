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

BUILD = "20261007-m3-same20-full-pipeline-recovery-v1"
PATCH = "s20-v12-dont-skip-discovery-on-weak-cache"


_OPEN_BIDS_ID = re.compile(
    r"(?:/open-bids/[^/?#]+|/solicitations)/(?P<id>\d{6,})(?:/|\?|$)",
    re.I,
)
_PRIVATE_VIEW = re.compile(
    r"/private/supplier/solicitations/(?P<id>\d{6,})",
    re.I,
)
_STATEWIDE_ID = re.compile(
    r"/solicitations/statewide/(?P<id>\d{6,})",
    re.I,
)


def is_statewide_bidnet_url(url: str) -> bool:
    u = (url or "").lower()
    return "bidnet" in u and "/statewide/" in u


def extract_statewide_id(url: str) -> str | None:
    m = _STATEWIDE_ID.search(url or "")
    return m.group("id") if m else None


def is_bidnet_download_endpoint(url: str) -> bool:
    """True for BidNet URLs that emit binaries (intercept/getFile/download), not HTML viewers."""
    u = (url or "").lower()
    if "bidnet" not in u:
        return False
    return bool(
        "/download/intercept" in u
        or "/abstract/download" in u
        or "/download?" in u
        or "getfile" in u
        or "fileid=" in u
        or "docid=" in u
        or re.search(r"\.(pdf|docx?|xlsx?|csv|zip)(?:$|\?)", u)
    )


def is_bidnet_detail_page_url(url: str) -> bool:
    u = (url or "").lower()
    if "bidnetdirect.com" not in u and "bidnet.com" not in u:
        return False
    # Intercept / getFile endpoints are downloadable binaries — never treat as detail HTML
    if is_bidnet_download_endpoint(u):
        return False
    if re.search(r"\.(pdf|docx?|xlsx?|csv|zip)(?:$|\?)", u):
        return False
    return bool(
        "/open-bids/" in u
        or "/private/supplier/solicitations/" in u
        or "/statewide/" in u
        or re.search(r"/solicitations/\d{6,}", u)
    )


def resolve_bidnet_private_detail_url(url: str) -> str | None:
    """Map public open-bids URLs to private supplier /view (auth session required)."""
    variants = resolve_bidnet_private_detail_url_candidates(url)
    return variants[0] if variants else None


def resolve_bidnet_private_detail_url_candidates(url: str) -> list[str]:
    """Candidate private detail URLs (with/without leading zeros, /view and /abstract).

    Statewide abstract IDs live in a different namespace than private solicitation
    IDs — never synthesize /private/.../{statewide_id}/view from them.
    """
    if not url or not url.startswith("http") or "bidnet" not in url.lower():
        return []
    # Statewide IDs ≠ private solicitation IDs
    if is_statewide_bidnet_url(url):
        return []
    sid: str | None = None
    m = _PRIVATE_VIEW.search(url) or _OPEN_BIDS_ID.search(url)
    if m:
        sid = m.group("id")
    else:
        # Avoid bare statewide-style long IDs already handled above
        m2 = re.search(r"/solicitations/(?!statewide/)(\d{6,})(?:/abstract|/view)?(?:\?|$)", url)
        if m2:
            sid = m2.group(1)
    if not sid:
        return []
    ids = [sid]
    stripped = sid.lstrip("0") or sid
    if stripped != sid:
        ids.append(stripped)
    out: list[str] = []
    for i in ids:
        for suffix in ("view", "abstract", "documents"):
            u = f"https://www.bidnetdirect.com/private/supplier/solicitations/{i}/{suffix}"
            if u not in out:
                out.append(u)
    return out


def extract_private_solicitation_refs_from_html(html: str | bytes, *, base_url: str = "") -> list[str]:
    """Pull real private/open-bids solicitation URLs out of authenticated HTML/JSON text."""
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="ignore")
    else:
        text = html or ""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    patterns = [
        r"https?://[^\"'\s]*bidnetdirect\.com/private/supplier/solicitations/\d{6,}(?:/(?:view|abstract|documents))?",
        r"/private/supplier/solicitations/(\d{6,})(?:/(?:view|abstract|documents))?",
        r"/[a-z\-]+/solicitations/open-bids/[^\"'\s]+/(\d{6,})",
        r"[\"']solicitationId[\"']\s*:\s*[\"']?(\d{6,})",
        r"[\"']solicitation(?:Number|No)?[\"']\s*:\s*[\"']?(\d{6,})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            g = m.group(0)
            if g.startswith("http"):
                u = g.split("?")[0].rstrip("\"'")
            elif g.startswith("/"):
                sid_m = re.search(r"(\d{6,})", g)
                if not sid_m:
                    continue
                if "/open-bids/" in g:
                    from urllib.parse import urljoin

                    u = urljoin(base_url or "https://www.bidnetdirect.com/", g.split("?")[0])
                else:
                    u = f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid_m.group(1)}/view"
            else:
                sid = m.group(1) if m.lastindex else None
                if not sid:
                    continue
                u = f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid}/view"
            if u not in seen:
                seen.add(u)
                found.append(u)
    return found[:20]

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


def _strip_bom(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:]
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data[2:]
    return data


def looks_like_html_bytes(data: bytes) -> bool:
    """True when bytes are an HTML/XML page (incl. UTF-8 BOM), not a binary attachment."""
    if not data or len(data) < 5:
        return False
    raw = _strip_bom(data)
    head = raw.lstrip()[:64].lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml")):
        return True
    sample = raw[:2500].lower()
    if b"<!doctype html" in sample or b"<html" in sample[:800]:
        return True
    try:
        text = raw[:800].decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip().lower()
    except Exception:
        return False
    return text.startswith(("<!doctype", "<html", "<head", "<?xml"))


def harvest_attachment_urls_from_html(html: bytes | str, *, base_url: str = "") -> list[dict[str, Any]]:
    """When BidNet returns a viewer/listing HTML page, pull real file links from it."""
    from urllib.parse import urljoin

    if isinstance(html, bytes):
        text = _strip_bom(html).decode("utf-8", errors="ignore")
    else:
        text = html or ""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    patterns = (
        r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|csv|zip)(?:\?[^"\']*)?)["\']',
        r'href=["\']([^"\']*(?:/download|/file|/attachment|/document|downloadDocument|getDocument|getFile|fileId|docId|documentId)[^"\']*)["\']',
        r'data-(?:url|download-url|file-url|document-url)=["\']([^"\']+)["\']',
        r'(https?://[^"\'\s<>]+\.(?:pdf|docx?|xlsx?|csv)(?:\?[^"\'\s<>]*)?)',
        r'["\'](/[^"\']*(?:SolicitationDocument|solicitation-document|bidDocument)[^"\']*)["\']',
    )
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            href = (m.group(1) or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            low = href.lower()
            if any(x in low for x in ("login", "logout", "register", "captcha", "authentication")):
                continue
            url = urljoin(base_url or "", href) if base_url else href
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            name = url.split("?")[0].rsplit("/", 1)[-1] or "attachment"
            if len(name) < 3 or name in {"download", "file", "attachment", "document"}:
                name = f"harvested_{len(found)+1}"
            ext = Path(name).suffix.lower().lstrip(".")
            found.append(
                {
                    "document_name": name[:160],
                    "filename": name[:160],
                    "document_url": url,
                    "url": url,
                    "source_url": url,
                    "extension": ext or None,
                    "retrieval_status": "URL_DISCOVERED_FROM_HTML",
                    "requires_auth": True,
                    "harvested_from_html": True,
                }
            )
            if len(found) >= 24:
                return found
    return found


def purge_html_document_caches(*, limit: int = 500) -> dict[str, Any]:
    """Delete poisoned HTML files under bidnet_auth/documents so re-download can run."""
    from m3_data_root import data_path

    root = data_path("bidnet_auth", "documents")
    removed = 0
    scanned = 0
    if not root.exists():
        return {"scanned": 0, "removed": 0, "root": str(root)}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        scanned += 1
        if scanned > limit * 4:
            break
        try:
            data = p.read_bytes()[:4096]
        except Exception:
            continue
        if looks_like_html_bytes(data):
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
            if removed >= limit:
                break
    return {"scanned": scanned, "removed": removed, "root": str(root), "patch": PATCH}


def _sig_ok(data: bytes, ext: str) -> tuple[bool, str | None]:
    if not data or len(data) < 8:
        return False, "empty_or_tiny"
    data = _strip_bom(data)
    head = data[:16]
    ext = (ext or "").lower().lstrip(".")
    # Sniff when extension missing/wrong (BidNet often saves as document_1)
    if not ext:
        if data[:5] == b"%PDF-":
            ext = "pdf"
        elif data[:2] == b"PK":
            ext = "xlsx"
        elif head[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
            ext = "xls"
    # HTML masquerading — reject for ANY claimed/sniffed extension (incl. blank / BOM)
    if looks_like_html_bytes(data):
        return False, "html_or_login_page_saved_as_binary"
    text_head = data[:400].decode("utf-8", errors="ignore")
    if _LOGIN_HTML.search(text_head):
        return False, "html_or_login_page_saved_as_binary"
    # Never accept bare markup as an "unknown" binary attachment
    stripped_bytes = data.lstrip()[:1]
    if stripped_bytes == b"<" and ext not in {"svg"}:
        return False, "markup_bytes_not_binary_attachment"
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
        if _LOGIN_HTML.search(sample) or looks_like_html_bytes(data):
            return False, "html_login_as_text"
        if sample.count(",") + sample.count("\t") + sample.count("\n") < 2:
            return False, "not_parseable_tabular_text"
        return True, None
    if ext in {"html", "htm"}:
        # HTML is never a product-schedule binary for materialization
        return False, "html_not_materializable_attachment"
    # Unknown extension — accept non-empty non-login / non-markup blobs
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


_SERVICE_TITLE = re.compile(
    r"\b(insurance|administrator|grant\s+program|community[- ]led|"
    r"\bcm\b|construction\s+manag|professional\s+services|"
    r"lighting\s+upgrade.*services|re[- ]pricing|intake\s+service)\b",
    re.I,
)
_CATALOG_TITLE = re.compile(
    r"\b(oem|parts?\s+and\s+(accessories|labor)|replacement\s+parts|"
    r"unit\s+parts|pump\s+parts|hvac\s+unit\s+parts)\b",
    re.I,
)


def _external_blocker_proven(
    free_chase_meta: dict[str, Any],
    invalid: list[dict[str, Any]],
    valid: int,
) -> bool:
    """True when BidNet wall + free chase exhausted (no public package available)."""
    if valid > 0:
        return False
    status = str((free_chase_meta or {}).get("status") or "")
    docs = int((free_chase_meta or {}).get("doc_count") or 0)
    chase_exhausted = status in {
        "PACKAGE_UNAVAILABLE_FREE",
        "PACKAGE_RECOVERY_RETRYABLE",
        "TIMEOUT",
        "",
    } and docs == 0
    wall = any(
        str(e.get("failure") or "")
        in {"bidnet_detail_page_not_binary", "html_or_login_page_saved_as_binary"}
        for e in invalid
    )
    return bool(wall and chase_exhausted)


def _classify_package_product(
    *,
    valid: int,
    invalid: list[dict[str, Any]],
    content_recognition: dict[str, Any],
    free_chase_meta: dict[str, Any],
    title: str,
) -> str:
    cr_clf = str(content_recognition.get("classification") or "")
    if cr_clf in {"LINES_RECOVERED", "CATALOG_DISCOUNT_ONLY", "PARSER_DEFECT_REMAINS"}:
        return cr_clf
    if valid == 0 and _SERVICE_TITLE.search(title or ""):
        return "NO_PRODUCT_LINES_ACTUALLY_PRESENT"
    if valid == 0 and _external_blocker_proven(free_chase_meta, invalid, valid):
        if _CATALOG_TITLE.search(title or ""):
            # OEM/parts RFQs behind BidNet wall with no free package — not itemizable here
            return "PRODUCT_SCHEDULE_INACCESSIBLE"
        return "PRODUCT_SCHEDULE_INACCESSIBLE"
    if valid == 0 and (invalid or free_chase_meta):
        return "PRODUCT_SCHEDULE_INACCESSIBLE"
    return cr_clf or "UNKNOWN"


def _operator_status_for_package(
    *,
    valid: int,
    invalid: list[dict[str, Any]],
    content_recognition: dict[str, Any],
    free_chase_meta: dict[str, Any],
    title: str,
) -> str:
    if content_recognition.get("operator_product_status") or content_recognition.get("operator_status"):
        if valid > 0:
            return str(
                content_recognition.get("operator_product_status")
                or content_recognition.get("operator_status")
            )
    if valid == 0 and _SERVICE_TITLE.search(title or ""):
        return (
            "This solicitation describes services, grants, or administration work "
            "rather than an itemized product list."
        )
    if _external_blocker_proven(free_chase_meta, invalid, valid):
        return (
            "BidNet requires a membership/portal login for the package documents, "
            "and no free public product schedule was found for this opportunity."
        )
    if valid == 0 and invalid:
        return (
            "BidNet returned web pages instead of downloadable bid attachments. "
            "M3 could not open a product schedule for this deal."
        )
    return str(
        content_recognition.get("operator_product_status")
        or content_recognition.get("operator_status")
        or ""
    )


def materialize_attachments(
    docs: list[dict[str, Any]],
    *,
    opportunity_id: str,
    client: Any | None = None,
    limit: int = 20,
    title: str | None = None,
    buyer: str | None = None,
    detail_url: str | None = None,
) -> dict[str, Any]:
    """Download missing attachments, validate content, return materialized index."""
    from m3_data_root import data_path

    working_docs = [d for d in (docs or []) if isinstance(d, dict)]
    opp_title = (title or "").strip() or opportunity_id
    seed_detail = str(detail_url or "").strip()
    index = build_attachment_index(working_docs, opportunity_id=opportunity_id)
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]+", "_", opportunity_id or "unknown")[:48]
    downloaded = 0
    valid = 0
    invalid: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []
    harvested_extra = 0
    page_discovered = 0
    free_chase_meta: dict[str, Any] = {}
    discovery_meta: dict[str, Any] = {
        "statewide": False,
        "statewide_id": None,
        "resolved_private": [],
        "title_queries": [],
        "page_docs": 0,
        "route": None,
        "disk_cache_hits": 0,
        "reauth": [],
        "patch": PATCH,
    }
    seen_urls = {
        str(e.get("source_url") or "").split("#")[0]
        for e in index
        if e.get("source_url")
    }

    def _save_html_diag(html_body: bytes, label: str) -> None:
        try:
            diag = data_path("bidnet_auth", "html_diag")
            diag.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{safe_sid}_{label}")[:80]
            (diag / f"{safe}.html").write_bytes(html_body[:80_000])
        except Exception:
            pass

    def _enqueue_docs(new_raw: list[dict[str, Any]], *, counter: str = "harvest") -> int:
        nonlocal harvested_extra, page_discovered, index
        added = 0
        kept: list[dict[str, Any]] = []
        for kid in new_raw:
            u = str(kid.get("document_url") or kid.get("source_url") or "").split("#")[0]
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            kept.append(kid)
            added += 1
            if counter == "page":
                page_discovered += 1
            else:
                harvested_extra += 1
        if not kept:
            return 0
        working_docs.extend(kept)
        index.extend(build_attachment_index(kept, opportunity_id=opportunity_id))
        return added

    def _enqueue_harvested(html_body: bytes, base_url: str) -> None:
        _save_html_diag(html_body, "harvest")
        kids = harvest_attachment_urls_from_html(html_body, base_url=base_url)
        _enqueue_docs(kids, counter="harvest")

    # Warm reuse: prior valid binaries for this opportunity (survives BidNet session blips)
    disk_cache_hits = 0
    try:
        cache_dir = data_path("bidnet_auth", "documents", safe_sid)
        if cache_dir.exists():
            seen_hashes = {
                str(e.get("CONTENT_HASH") or e.get("content_hash") or "")
                for e in materialized
            }
            for p in sorted(cache_dir.iterdir()):
                if not p.is_file() or p.stat().st_size < 32:
                    continue
                # Skip browser_dl staging noise unless it validates
                v = validate_local_file(p, claimed_ext=p.suffix.lstrip("."))
                if not v.get("DOCUMENT_CONTENT_VALID"):
                    continue
                h = str(v.get("content_hash") or "")
                if h and h in seen_hashes:
                    continue
                if h:
                    seen_hashes.add(h)
                entry = {
                    "filename": p.name,
                    "extension": p.suffix.lstrip(".").lower(),
                    "LOCAL_PATH": str(p),
                    "local_path": str(p),
                    "CONTENT_HASH": h,
                    "BYTE_SIZE": v.get("byte_size"),
                    "DOCUMENT_CONTENT_VALID": True,
                    "retrieval_status": "DISK_CACHE",
                    "SOURCE_URL": "",
                    # Weak tokens like "item"/"material" in hashed cache names must NOT
                    # count as schedule candidates or discovery gets skipped entirely.
                    "high_value": bool(
                        re.search(
                            r"pric(?:e|ing)|bid\s*form|bid\s*schedule|line\s*item|"
                            r"bom|\.xlsx|\.xls|\.csv|\.zip|cost\s*sheet|unit\s*price",
                            p.name,
                            re.I,
                        )
                    ),
                }
                materialized.append(entry)
                valid += 1
                downloaded += 1
                disk_cache_hits += 1
                if valid >= limit:
                    break
    except Exception:
        disk_cache_hits = 0
    discovery_meta["disk_cache_hits"] = disk_cache_hits

    html_wall_streak = 0

    def _maybe_reauth(reason: str) -> None:
        nonlocal html_wall_streak
        if client is None or not hasattr(client, "ensure_authenticated"):
            return
        html_wall_streak += 1
        if html_wall_streak < 2:
            return
        try:
            auth = client.ensure_authenticated()
            discovery_meta.setdefault("reauth", []).append(
                {"reason": reason, "ok": bool(getattr(auth, "authenticated", False))}
            )
            html_wall_streak = 0
        except Exception as exc:
            discovery_meta.setdefault("reauth", []).append(
                {"reason": reason, "error": f"{type(exc).__name__}"[:80]}
            )

    # Prefer high-value first; allow index to grow via HTML harvest / page discovery
    i = 0
    while i < len(index) and i < max(limit, 12) + harvested_extra + page_discovered:
        entry = index[i]
        i += 1
        if len(materialized) >= limit and harvested_extra == 0:
            break
        raw = entry.get("_raw") if isinstance(entry.get("_raw"), dict) else {}
        path_s = entry.get("local_path") or raw.get("local_path")
        # Re-validate existing
        if path_s and Path(path_s).exists():
            try:
                existing_bytes = Path(path_s).read_bytes()
            except Exception:
                existing_bytes = b""
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
                html_wall_streak = 0
            else:
                invalid.append({**entry, "failure": v.get("reason")})
                # HTML poison: harvest real attachment links before deleting cache
                if looks_like_html_bytes(existing_bytes):
                    _maybe_reauth("existing_html_wall")
                    _enqueue_harvested(existing_bytes, str(entry.get("source_url") or ""))
                try:
                    Path(path_s).unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                path_s = None
            if path_s:
                continue

        url = str(entry.get("source_url") or "")
        if not url.startswith("http") or client is None or not hasattr(client, "download_bytes"):
            entry["access_class"] = classify_access_blocker(entry)
            if entry.get("downloadable"):
                entry["blocker"] = "SCHEDULE_DISCOVERED_NOT_DOWNLOADABLE" if entry.get("high_value") else "DOWNLOAD_FAILED"
            continue
        # Public open-bids "document" URLs are detail pages, not binaries — skip byte fetch
        if is_bidnet_detail_page_url(url):
            entry["DOCUMENT_CONTENT_VALID"] = False
            entry["DOCUMENT_VALIDATION_FAILURE_REASON"] = "bidnet_detail_page_not_binary"
            entry["retrieval_status"] = "DETAIL_PAGE"
            invalid.append({**entry, "failure": "bidnet_detail_page_not_binary"})
            continue
        try:
            body = client.download_bytes(url, timeout_ms=60_000)
        except Exception as exc:
            entry["retrieval_status"] = "DOWNLOAD_FAILED"
            entry["access_class"] = classify_access_blocker(entry, error=str(exc))
            entry["blocker"] = "DOWNLOAD_FAILED"
            entry["error"] = f"{type(exc).__name__}:{exc}"[:200]
            _maybe_reauth(f"download_exc:{type(exc).__name__}")
            continue
        # Empty BidNet API responses: try plain HTTP (agency / intercept URLs)
        if not body or len(body) < 32:
            try:
                import httpx as _httpx

                resp = _httpx.get(url, timeout=45, follow_redirects=True)
                if resp.status_code < 400 and resp.content and len(resp.content) >= 32:
                    body = resp.content
            except Exception:
                pass
        name = entry.get("filename") or "doc"
        ext = entry.get("extension") or Path(name).suffix.lower().lstrip(".")
        v = _sig_ok(body or b"", ext)
        h = hashlib.sha256(body or b"").hexdigest()[:16]
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(name))[:80]
        path = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
        path.parent.mkdir(parents=True, exist_ok=True)
        downloaded += 1
        if v[0] and body:
            path.write_bytes(body)
            entry["LOCAL_PATH"] = str(path)
            entry["CONTENT_HASH"] = h
            entry["BYTE_SIZE"] = len(body or b"")
            entry["DOWNLOAD_TIME"] = now_utc().isoformat()
            entry["SOURCE_URL"] = url
            entry["SOURCE_DOCUMENT_ID"] = entry.get("document_id")
            entry["MIME_TYPE"] = entry.get("mime_type")
            entry["DOCUMENT_CONTENT_VALID"] = True
            entry["DOCUMENT_VALIDATION_FAILURE_REASON"] = None
            entry["retrieval_status"] = "DOWNLOADED"
            # Mirror onto raw doc for downstream extractors
            raw["local_path"] = str(path)
            raw["content_hash"] = h
            raw["retrieval_status"] = entry["retrieval_status"]
            raw["retrieved_at"] = entry["DOWNLOAD_TIME"]
            raw["size_bytes"] = entry["BYTE_SIZE"]
            valid += 1
            materialized.append(entry)
            html_wall_streak = 0
        else:
            entry["DOCUMENT_CONTENT_VALID"] = False
            entry["DOCUMENT_VALIDATION_FAILURE_REASON"] = v[1]
            entry["retrieval_status"] = "INVALID_CONTENT"
            entry["BYTE_SIZE"] = len(body or b"")
            entry["SOURCE_URL"] = url
            invalid.append({**entry, "failure": v[1]})
            if body and looks_like_html_bytes(body):
                _maybe_reauth("download_html_wall")
                _enqueue_harvested(body, url)
            # Do not persist HTML/invalid bytes on disk
            try:
                if path.exists():
                    path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass

    # DOM-scrape BidNet when package lacks a real schedule candidate.
    # Disk-cache cover PDFs often match weak "item/material" tokens — ignore those.
    def _is_schedule_candidate(e: dict[str, Any]) -> bool:
        fn = str(e.get("filename") or e.get("document_name") or "")
        ext = str(e.get("extension") or Path(fn).suffix.lstrip(".")).lower()
        if ext in {"xlsx", "xls", "csv"}:
            return True
        if e.get("high_value") and re.search(
            r"pric(?:e|ing)|bid\s*form|bid\s*schedule|line\s*item|bom|"
            r"cost\s*sheet|unit\s*price|\.xlsx|\.xls|\.csv|\.zip",
            fn,
            re.I,
        ):
            return True
        return bool(
            re.search(
                r"pric(?:e|ing)|bid\s*form|bid\s*schedule|line\s*item|bom|"
                r"cost\s*sheet|unit\s*price|\.xlsx|\.xls|\.csv|\.zip",
                fn,
                re.I,
            )
        )

    has_schedule_candidate = any(_is_schedule_candidate(e) for e in materialized)
    need_discovery = valid == 0 or not has_schedule_candidate
    discovery_meta["has_schedule_candidate"] = has_schedule_candidate
    discovery_meta["need_discovery"] = need_discovery
    if need_discovery and client is not None and hasattr(client, "discover_attachment_links"):
        detail_candidates: list[str] = []
        statewide_seeds: list[str] = []
        if seed_detail.startswith("http"):
            if is_statewide_bidnet_url(seed_detail):
                statewide_seeds.append(seed_detail)
            else:
                detail_candidates.extend(resolve_bidnet_private_detail_url_candidates(seed_detail) or [])
            detail_candidates.append(seed_detail)
        for e in index:
            u = str(e.get("source_url") or "")
            if not (u.startswith("http") and "bidnet" in u.lower()):
                continue
            if is_statewide_bidnet_url(u):
                statewide_seeds.append(u)
            else:
                detail_candidates.extend(resolve_bidnet_private_detail_url_candidates(u) or [])
            if is_bidnet_detail_page_url(u):
                detail_candidates.append(u)
        # Prefer private /view first (never synthetic statewide→private)
        detail_url = ""
        seen_d: set[str] = set()
        uniq_details: list[str] = []
        for u in detail_candidates:
            if u and u not in seen_d and not is_statewide_bidnet_url(u):
                seen_d.add(u)
                uniq_details.append(u)
        for u in uniq_details:
            if "/private/supplier/solicitations/" in u and u.rstrip("/").endswith("/view"):
                detail_url = u
                break
        if not detail_url and uniq_details:
            detail_url = uniq_details[0]
        sw_id = None
        for su in statewide_seeds:
            sw_id = extract_statewide_id(su)
            if sw_id:
                break
        discovery_meta["statewide"] = bool(statewide_seeds)
        discovery_meta["statewide_id"] = sw_id
        if detail_url or opp_title or statewide_seeds:
            try:
                page_docs: list[dict[str, Any]] = []
                seen_doc_keys: set[str] = set()
                routes_hit: list[str] = []

                def _absorb(docs: list[dict[str, Any]] | None, route: str) -> int:
                    nonlocal page_docs
                    added_n = 0
                    for d in docs or []:
                        if not isinstance(d, dict):
                            continue
                        key = str(
                            d.get("local_path")
                            or d.get("document_url")
                            or d.get("source_url")
                            or d.get("url")
                            or ""
                        )
                        if not key or key in seen_doc_keys:
                            continue
                        seen_doc_keys.add(key)
                        page_docs.append(d)
                        added_n += 1
                    if added_n:
                        routes_hit.append(f"{route}+{added_n}")
                    return added_n

                def _docs_look_thin() -> bool:
                    if len(page_docs) < 3:
                        return True
                    scheduleish = 0
                    for d in page_docs:
                        fn = str(d.get("filename") or d.get("document_name") or "")
                        if d.get("local_path"):
                            scheduleish += 1
                        if re.search(
                            r"pric|schedule|bid\s*form|item|bom|\.xlsx|\.xls|\.csv|\.zip",
                            fn,
                            re.I,
                        ):
                            scheduleish += 2
                    return scheduleish < 2

                # Fast path: take first decent hit; expand only when thin (cover/notice PDFs).
                if statewide_seeds and hasattr(client, "discover_attachments_from_statewide"):
                    for su in statewide_seeds[:1]:
                        _absorb(
                            client.discover_attachments_from_statewide(
                                su, title=opp_title, statewide_id=sw_id
                            ),
                            "statewide_resolve",
                        )
                        if not _docs_look_thin():
                            break
                if _docs_look_thin() and detail_url:
                    _absorb(client.discover_attachment_links(detail_url), "private_detail")
                search_queries: list[str] = []
                if sw_id:
                    search_queries.append(sw_id)
                if opp_title and len(opp_title.strip()) >= 8:
                    search_queries.append(opp_title.strip()[:120])
                discovery_meta["title_queries"] = search_queries
                if (
                    _docs_look_thin()
                    and search_queries
                    and hasattr(client, "discover_attachments_by_title")
                ):
                    for q in search_queries[:2]:
                        _absorb(
                            client.discover_attachments_by_title(q),
                            f"title_search:{q[:40]}",
                        )
                        if not _docs_look_thin():
                            break
                if _docs_look_thin():
                    for e in list(invalid)[:4]:
                        u = str(e.get("SOURCE_URL") or e.get("source_url") or "")
                        if "bidnet" not in u.lower():
                            continue
                        try:
                            html_b = (
                                client.download_bytes(u, timeout_ms=30_000)
                                if hasattr(client, "download_bytes")
                                else None
                            )
                        except Exception:
                            html_b = None
                        if not html_b or not looks_like_html_bytes(html_b):
                            continue
                        refs = extract_private_solicitation_refs_from_html(html_b, base_url=u)
                        discovery_meta["resolved_private"] = refs[:5]
                        for priv in refs[:2]:
                            _absorb(client.discover_attachment_links(priv), "html_ref")
                            if not _docs_look_thin():
                                break
                        if not _docs_look_thin():
                            break
                discovery_meta["page_docs"] = len(page_docs or [])
                discovery_meta["routes_hit"] = routes_hit
                discovery_meta["browser_local"] = sum(
                    1 for d in page_docs if d.get("local_path")
                )
                discovery_meta["route"] = (
                    routes_hit[0].split("+", 1)[0] if routes_hit else None
                )
                # Prefer schedule-like / browser-captured binaries when enqueueing
                page_docs.sort(
                    key=lambda d: (
                        0 if d.get("local_path") else 1,
                        0
                        if re.search(
                            r"pric|schedule|bid|item|bom|equip|material|spec|\.xlsx|\.xls|\.zip",
                            str(d.get("filename") or d.get("document_name") or ""),
                            re.I,
                        )
                        else 1,
                        str(d.get("filename") or ""),
                    )
                )
                added = _enqueue_docs(page_docs, counter="page")
                # Prefer schedule-like binaries over BidNet viewer HTML for remaining downloads
                if added and i < len(index):
                    def _dl_prio(e: dict[str, Any]) -> tuple[int, str]:
                        fn = str(e.get("filename") or e.get("source_url") or "").lower()
                        url = str(e.get("source_url") or "")
                        score = 50
                        if is_bidnet_detail_page_url(url):
                            score += 100
                        if any(x in fn for x in (".xlsx", ".xls", ".csv")):
                            score -= 40
                        if any(
                            x in fn
                            for x in (
                                "price",
                                "pricing",
                                "bid",
                                "schedule",
                                "item",
                                "bom",
                                "equipment",
                                "material",
                                "spec",
                                "form",
                            )
                        ):
                            score -= 20
                        if fn.endswith(".pdf") or ".pdf" in fn:
                            score -= 10
                        if is_bidnet_download_endpoint(url):
                            score -= 30
                        return (score, fn)

                    tail = index[i:]
                    tail.sort(key=_dl_prio)
                    index[i:] = tail
                # Continue download loop for newly discovered links
                if added:
                    already_ok = {
                        str(m.get("SOURCE_URL") or m.get("source_url") or "")
                        for m in materialized
                    }
                    while i < len(index) and i < max(limit, 12) + harvested_extra + page_discovered:
                        entry = index[i]
                        i += 1
                        raw = entry.get("_raw") if isinstance(entry.get("_raw"), dict) else {}
                        # Browser click-download may already have a validated local file
                        path_s = entry.get("local_path") or raw.get("local_path")
                        if path_s and Path(str(path_s)).exists():
                            v = validate_local_file(path_s, claimed_ext=entry.get("extension"))
                            if v.get("DOCUMENT_CONTENT_VALID"):
                                entry["LOCAL_PATH"] = str(path_s)
                                entry["CONTENT_HASH"] = v.get("content_hash")
                                entry["BYTE_SIZE"] = v.get("byte_size")
                                entry["DOCUMENT_CONTENT_VALID"] = True
                                entry["retrieval_status"] = "BROWSER_DOWNLOAD"
                                entry["SOURCE_URL"] = entry.get("source_url") or str(path_s)
                                valid += 1
                                downloaded += 1
                                materialized.append(entry)
                                already_ok.add(str(entry.get("source_url") or path_s))
                                continue
                        url = str(entry.get("source_url") or "")
                        if not url.startswith("http") or url in already_ok:
                            continue
                        if is_bidnet_detail_page_url(url):
                            continue
                        try:
                            body = client.download_bytes(url, timeout_ms=60_000)
                        except Exception:
                            continue
                        name = entry.get("filename") or "doc"
                        ext = entry.get("extension") or Path(str(name)).suffix.lower().lstrip(".")
                        ok, reason = _sig_ok(body or b"", ext)
                        downloaded += 1
                        if ok and body:
                            h = hashlib.sha256(body).hexdigest()[:16]
                            safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(name))[:80]
                            path = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(body)
                            entry["LOCAL_PATH"] = str(path)
                            entry["CONTENT_HASH"] = h
                            entry["BYTE_SIZE"] = len(body)
                            entry["DOWNLOAD_TIME"] = now_utc().isoformat()
                            entry["SOURCE_URL"] = url
                            entry["DOCUMENT_CONTENT_VALID"] = True
                            entry["retrieval_status"] = "DOWNLOADED"
                            raw["local_path"] = str(path)
                            raw["content_hash"] = h
                            raw["retrieval_status"] = "DOWNLOADED"
                            valid += 1
                            materialized.append(entry)
                            already_ok.add(url)
                        else:
                            invalid.append({**entry, "failure": reason})
                            if body and looks_like_html_bytes(body):
                                _maybe_reauth("discovery_html_wall")
                                _enqueue_harvested(body, url)
            except Exception as exc:
                discovery_meta["error"] = f"{type(exc).__name__}:{exc}"[:160]

    # BidNet membership / portal wall / empty downloads → chase free public package documents
    bidnet_wall = any(
        str(e.get("failure") or e.get("DOCUMENT_VALIDATION_FAILURE_REASON") or "")
        in {
            "bidnet_detail_page_not_binary",
            "html_or_login_page_saved_as_binary",
            "empty_or_tiny",
        }
        or "subscription" in str(e.get("SOURCE_URL") or e.get("source_url") or "").lower()
        for e in invalid
    )
    if valid == 0 or bidnet_wall:
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
            from bidnet_recovery.free_package_chase import chase_free_package

            chase_detail = seed_detail or next(
                (
                    str(e.get("source_url") or "")
                    for e in index
                    if is_bidnet_detail_page_url(str(e.get("source_url") or ""))
                ),
                None,
            )
            # candidate_free_urls: non-BidNet agency pages + intercept downloads
            candidate_free = []
            for e in index:
                u = str(e.get("source_url") or "")
                if not u.startswith("http"):
                    continue
                if "bidnet" not in u.lower() or "intercept" in u.lower() or "download" in u.lower():
                    candidate_free.append(u)
            if seed_detail and seed_detail not in candidate_free:
                candidate_free.insert(0, seed_detail)
            rec = {
                "title": opp_title,
                "canonical_opportunity_id": opportunity_id,
                "buyer": buyer,
                "attachments_metadata": working_docs,
                "detail_url": chase_detail,
                "authoritative_url": seed_detail or chase_detail,
                "candidate_free_urls": candidate_free[:12],
            }

            def _run_chase() -> dict[str, Any]:
                return chase_free_package(rec, refresh_overview=True)

            # Bound free chase so one portal cannot stall the same-13 walker
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_run_chase)
                try:
                    chase = fut.result(timeout=90)
                except FuturesTimeout:
                    free_chase_meta = {"status": "TIMEOUT", "doc_count": 0, "route": None}
                    chase = {}
            if chase:
                free_chase_meta = {
                    "status": chase.get("status") or chase.get("chase_status"),
                    "doc_count": len(chase.get("documents") or []),
                    "route": chase.get("recovery_route") or chase.get("matched_source"),
                }
            chase_docs = [
                {
                    "document_name": d.get("document_name") or d.get("filename") or "free_doc",
                    "filename": d.get("document_name") or d.get("filename") or "free_doc",
                    "document_url": d.get("document_url") or d.get("url") or d.get("download_url"),
                    "url": d.get("document_url") or d.get("url") or d.get("download_url"),
                    "source_url": d.get("document_url") or d.get("url") or d.get("download_url"),
                    "free_chase": True,
                }
                for d in (chase.get("documents") or [])
                if isinstance(d, dict)
                and (d.get("document_url") or d.get("url") or d.get("download_url"))
            ]
            if chase_docs:
                added = _enqueue_docs(chase_docs, counter="page")
                free_chase_meta["enqueued"] = added
                already_ok = {
                    str(m.get("SOURCE_URL") or m.get("source_url") or "")
                    for m in materialized
                }
                while i < len(index) and i < max(limit, 16) + harvested_extra + page_discovered:
                    entry = index[i]
                    i += 1
                    raw = entry.get("_raw") if isinstance(entry.get("_raw"), dict) else {}
                    url = str(entry.get("source_url") or "")
                    if not url.startswith("http") or url in already_ok or is_bidnet_detail_page_url(url):
                        continue
                    if client is None or not hasattr(client, "download_bytes"):
                        # Non-BidNet free URLs: plain HTTP
                        try:
                            import httpx as _httpx

                            resp = _httpx.get(url, timeout=45, follow_redirects=True)
                            body = resp.content if resp.status_code < 400 else None
                        except Exception:
                            continue
                    else:
                        try:
                            body = client.download_bytes(url, timeout_ms=60_000)
                        except Exception:
                            # Free CDN/agency URLs often work without BidNet session
                            try:
                                import httpx as _httpx

                                resp = _httpx.get(url, timeout=45, follow_redirects=True)
                                body = resp.content if resp.status_code < 400 else None
                            except Exception:
                                continue
                    name = entry.get("filename") or "free_doc"
                    ext = entry.get("extension") or Path(str(name)).suffix.lower().lstrip(".")
                    ok, reason = _sig_ok(body or b"", ext)
                    downloaded += 1
                    if ok and body:
                        h = hashlib.sha256(body).hexdigest()[:16]
                        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(name))[:80]
                        path = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(body)
                        entry["LOCAL_PATH"] = str(path)
                        entry["CONTENT_HASH"] = h
                        entry["BYTE_SIZE"] = len(body)
                        entry["DOWNLOAD_TIME"] = now_utc().isoformat()
                        entry["SOURCE_URL"] = url
                        entry["DOCUMENT_CONTENT_VALID"] = True
                        entry["retrieval_status"] = "DOWNLOADED"
                        raw["local_path"] = str(path)
                        raw["retrieval_status"] = "DOWNLOADED"
                        valid += 1
                        materialized.append(entry)
                        already_ok.add(url)
                    else:
                        invalid.append({**entry, "failure": reason})
        except Exception as exc:
            free_chase_meta = {"error": f"{type(exc).__name__}:{exc}"[:160]}

    # Expand BidNet package ZIPs into member PDFs/XLSX before recognition
    zip_expanded = 0
    try:
        expanded_entries: list[dict[str, Any]] = []
        for e in list(materialized):
            path_s = e.get("LOCAL_PATH") or e.get("local_path")
            if not path_s:
                continue
            path = Path(str(path_s))
            if not path.exists():
                continue
            fname = str(e.get("filename") or path.name).lower()
            if fname.endswith((".xlsx", ".docx", ".xls")):
                continue
            try:
                head = path.read_bytes()[:8]
            except Exception:
                continue
            looks_zip = fname.endswith(".zip") or head[:2] == b"PK"
            if not looks_zip:
                continue
            try:
                if not zipfile.is_zipfile(path):
                    continue
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
                    # Skip OpenXML workbooks/docs (already valid attachments)
                    if any(n.startswith("xl/") or n.startswith("word/") for n in names):
                        continue
                    for info in zf.infolist()[:40]:
                        if info.is_dir() or info.filename.startswith("__MACOSX"):
                            continue
                        member = Path(info.filename).name
                        ext = Path(member).suffix.lower().lstrip(".")
                        if ext not in {"pdf", "xlsx", "xls", "csv", "docx", "doc"}:
                            continue
                        try:
                            body = zf.read(info)
                        except Exception:
                            continue
                        ok, _reason = _sig_ok(body or b"", ext)
                        if not ok or not body:
                            continue
                        h = hashlib.sha256(body).hexdigest()[:16]
                        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", member)[:80]
                        out = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
                        out.parent.mkdir(parents=True, exist_ok=True)
                        if not out.exists():
                            out.write_bytes(body)
                        entry = {
                            "filename": member,
                            "extension": ext,
                            "LOCAL_PATH": str(out),
                            "local_path": str(out),
                            "CONTENT_HASH": h,
                            "BYTE_SIZE": len(body),
                            "SOURCE_URL": e.get("SOURCE_URL") or e.get("source_url"),
                            "DOCUMENT_CONTENT_VALID": True,
                            "retrieval_status": "ZIP_EXPANDED",
                            "high_value": bool(
                                re.search(
                                    r"pric|bid\s*form|schedule|item|bom|equipment|material|spec",
                                    member,
                                    re.I,
                                )
                            ),
                            "document_role_guess": "GENERIC_ATTACHMENT",
                            "zip_parent": str(path),
                        }
                        expanded_entries.append(entry)
                        zip_expanded += 1
            except Exception:
                continue
        if expanded_entries:
            # Prefer schedule-like members first for recognition
            expanded_entries.sort(key=lambda x: (0 if x.get("high_value") else 1, str(x.get("filename") or "")))
            materialized.extend(expanded_entries)
            valid = len([m for m in materialized if m.get("DOCUMENT_CONTENT_VALID") or m.get("LOCAL_PATH")])
    except Exception:
        zip_expanded = 0

    # Content-first recognition — inspect every valid local file (filename secondary)
    content_recognition: dict[str, Any] = {}
    try:
        from bidnet_engine.schedule_content_recognition import inspect_package_documents

        # Cap inspection volume so one huge PDF cannot stall the same-13 walker
        # Prefer high-value / spreadsheet members when many files exist
        inspect_docs = sorted(
            [
                e
                for e in materialized
                if e.get("LOCAL_PATH") or e.get("local_path")
            ],
            key=lambda e: (
                0
                if str(e.get("extension") or "").lower() in {"xlsx", "xls", "csv"}
                else 1,
                0 if e.get("high_value") else 1,
                0
                if re.search(
                    r"pric|bid|schedule|item|bom|equipment|material|spec|form",
                    str(e.get("filename") or ""),
                    re.I,
                )
                else 1,
            ),
        )[:16]
        content_recognition = inspect_package_documents(
            [
                {
                    "local_path": e.get("LOCAL_PATH") or e.get("local_path"),
                    # Prefer real filename+extension so PDF sniffing is not required
                    "filename": (
                        e.get("filename")
                        if str(e.get("filename") or "").lower().endswith(
                            (".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc")
                        )
                        else (
                            f"{e.get('filename') or 'document'}.{e.get('extension')}"
                            if e.get("extension")
                            else e.get("filename")
                        )
                    ),
                    "document_id": e.get("document_id"),
                    "source_url": e.get("source_url"),
                }
                for e in inspect_docs
            ]
        )
        # Stamp content roles onto materialized entries
        by_path = {
            str(i.get("path") or ""): i
            for i in (content_recognition.get("full_inspections") or [])
        }
        for e in materialized:
            insp = by_path.get(str(e.get("LOCAL_PATH") or e.get("local_path") or ""))
            if not insp:
                continue
            e["document_role_content"] = insp.get("document_role_content")
            e["role_confidence"] = insp.get("role_confidence")
            e["content_authority_score"] = insp.get("authority_score")
            e["expected_product_lines"] = insp.get("expected_product_lines")
            e["extracted_line_count"] = insp.get("extracted_line_count")
            e["product_signal_pages"] = insp.get("product_signal_pages")
            e["content_classification"] = insp.get("classification")
            if insp.get("is_product_like"):
                e["document_role_guess"] = insp.get("document_role_content") or e.get("document_role_guess")
                e["high_value"] = True
    except Exception as exc:
        content_recognition = {"error": f"{type(exc).__name__}:{exc}"[:200]}

    # Amendment graph (filename heuristics) + content authority
    amendments = [e for e in index if e.get("document_role_guess") == "AMENDMENT" or "addend" in str(e.get("filename") or "").lower()]
    product_roles = {
        "PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM",
        "PRODUCT_SCHEDULE", "ITEM_LIST", "SPECIFICATION_WITH_PRODUCT_TABLE",
        "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES", "CATALOG_REFERENCE",
    }
    pricing = [
        e for e in materialized
        if e.get("document_role_guess") in product_roles
        or e.get("document_role_content") in product_roles
        or int(e.get("extracted_line_count") or 0) >= 1
    ]
    for e in pricing:
        e["CURRENT_AUTHORITATIVE"] = True
        e["SUPERSEDES"] = None
        e["SUPERSEDED_BY"] = None
    if len(pricing) >= 2:
        def _rank(e: dict[str, Any]) -> float:
            n = str(e.get("filename") or "").lower()
            score = float(e.get("content_authority_score") or 0)
            if "revis" in n or "final" in n or "updated" in n:
                score += 3
            if "addend" in n or "amend" in n:
                score += 2
            if e.get("extension") in {"xlsx", "xls", "csv"}:
                score += 2
            score += min(int(e.get("extracted_line_count") or 0), 40) * 0.1
            return score

        pricing_sorted = sorted(pricing, key=_rank, reverse=True)
        for e in pricing_sorted[1:]:
            e["CURRENT_AUTHORITATIVE"] = False
            e["SUPERSEDED_BY"] = pricing_sorted[0].get("document_id")
        pricing_sorted[0]["SUPERSEDES"] = [e.get("document_id") for e in pricing_sorted[1:]]
        pricing_sorted[0]["CURRENT_AUTHORITATIVE"] = True

    auth_doc = None
    # Prefer content-recognition winner when it recovered lines
    auth_from_content = content_recognition.get("AUTHORITATIVE_PRODUCT_DOC") if isinstance(content_recognition, dict) else None
    if auth_from_content and content_recognition.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"):
        auth_path = str(auth_from_content.get("path") or "")
        for e in materialized:
            if str(e.get("LOCAL_PATH") or e.get("local_path") or "") == auth_path:
                auth_doc = e
                e["CURRENT_AUTHORITATIVE"] = True
                break
    if not auth_doc:
        for e in materialized:
            if e.get("CURRENT_AUTHORITATIVE") and (
                e.get("document_role_guess") in product_roles
                or int(e.get("extracted_line_count") or 0) >= 1
            ):
                auth_doc = e
                break
    if not auth_doc:
        for e in materialized:
            if e.get("high_value") or e.get("document_role_guess") == "GENERIC_ATTACHMENT":
                if e.get("extension") in {"xlsx", "xls", "csv", "docx", "pdf"}:
                    auth_doc = e
                    break
    if not auth_doc and materialized:
        for e in materialized:
            if e.get("extension") in {"xlsx", "xls", "csv"}:
                auth_doc = e
                break
    # Content found product lines even if role unclear — still authoritative
    if not auth_doc:
        for e in sorted(materialized, key=lambda x: int(x.get("extracted_line_count") or 0), reverse=True):
            if int(e.get("extracted_line_count") or 0) >= 1:
                auth_doc = e
                e["CURRENT_AUTHORITATIVE"] = True
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
    product_clf = _classify_package_product(
        valid=valid,
        invalid=invalid,
        content_recognition=content_recognition,
        free_chase_meta=free_chase_meta,
        title=opp_title,
    )
    # HTML/detail-page rejects after BidNet wall + free chase are access evidence, not soft parser bugs
    if product_clf in {
        "PRODUCT_SCHEDULE_INACCESSIBLE",
        "NO_PRODUCT_LINES_ACTUALLY_PRESENT",
        "CATALOG_DISCOUNT_ONLY",
    } and blocker in {"INVALID_DOWNLOADED_FILE", "DOWNLOAD_FAILED", "OTHER"}:
        blocker = (
            "SCHEDULE_NOT_PRESENT"
            if product_clf == "NO_PRODUCT_LINES_ACTUALLY_PRESENT"
            else "PRODUCT_SCHEDULE_INACCESSIBLE"
        )

    return {
        "build": BUILD,
        "patch": PATCH,
        "opportunity_id": opportunity_id,
        "PACKAGE_ATTACHMENT_INDEX": [{k: v for k, v in e.items() if k != "_raw"} for e in index],
        "materialized": [{k: v for k, v in e.items() if k != "_raw"} for e in materialized],
        "invalid_downloads": [{k: v for k, v in e.items() if k != "_raw"} for e in invalid],
        "HARVESTED_FROM_HTML": harvested_extra,
        "PAGE_DISCOVERED_ATTACHMENTS": page_discovered,
        "FREE_CHASE": free_chase_meta,
        "DISCOVERY": discovery_meta,
        "ZIP_EXPANDED_MEMBERS": zip_expanded,
        "PACKAGE_DOCUMENT_COUNT_EXPECTED": expected,
        "PACKAGE_DOCUMENT_COUNT_DISCOVERED": discovered,
        "PACKAGE_DOCUMENT_COUNT_DOWNLOADED": downloaded,
        "PACKAGE_DOCUMENT_COUNT_MATERIALIZED": valid,
        "PACKAGE_COMPLETENESS": completeness,
        "package_state_truthful": package_state,
        "PACKAGE_READY_FOR_LINE_EXTRACTION": ready,
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": bool(
            (
                auth_doc
                and (
                    int(auth_doc.get("extracted_line_count") or 0) >= 1
                    or auth_doc.get("document_role_content") in {
                        "PRICING_SCHEDULE", "PRODUCT_SCHEDULE", "BID_FORM", "ITEM_LIST",
                        "SPECIFICATION_WITH_PRODUCT_TABLE", "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES",
                        "LINE_ITEM_SCHEDULE", "BOM",
                    }
                    or content_recognition.get("AUTHORITATIVE_PRODUCT_DOC_FOUND")
                )
            )
            or int(content_recognition.get("EXTRACTED_PRODUCT_LINES") or 0) >= 1
            or int(content_recognition.get("AUTHORITATIVE_PRODUCT_DOC_FOUND") and 1 or 0) == 1
        ),
        "AUTHORITATIVE_PRODUCT_DOC_ID": (auth_doc or {}).get("document_id"),
        "AUTHORITATIVE_PRODUCT_DOC_ROLE": (auth_doc or {}).get("document_role_content")
        or (auth_doc or {}).get("document_role_guess"),
        "AUTHORITATIVE_PRODUCT_DOC": {k: v for k, v in (auth_doc or {}).items() if k != "_raw"} if auth_doc else None,
        "content_recognition": {
            k: v
            for k, v in (content_recognition or {}).items()
            if k not in {"full_inspections", "authoritative_rows"}
        },
        "EXPECTED_PRODUCT_LINES": content_recognition.get("EXPECTED_PRODUCT_LINES"),
        "EXTRACTED_PRODUCT_LINES": content_recognition.get("EXTRACTED_PRODUCT_LINES"),
        "LINE_EXTRACTION_COVERAGE": content_recognition.get("LINE_EXTRACTION_COVERAGE"),
        "product_classification": product_clf,
        "operator_product_status": (
            _operator_status_for_package(
                valid=valid,
                invalid=invalid,
                content_recognition=content_recognition,
                free_chase_meta=free_chase_meta,
                title=opp_title,
            )
        ),
        "external_blocker_proven": _external_blocker_proven(free_chase_meta, invalid, valid),
        "content_schedule_rows": content_recognition.get("authoritative_rows") or [],
        "invalid_download_reasons": [
            {"filename": e.get("filename"), "failure": e.get("failure") or e.get("DOCUMENT_VALIDATION_FAILURE_REASON")}
            for e in invalid[:12]
        ],
        "amendments_count": len(amendments),
        "primary_blocker": blocker,
        "docs_for_extraction": [
            {
                **(e.get("_raw") or {}),
                "local_path": e.get("LOCAL_PATH") or e.get("local_path"),
                "filename": e.get("filename"),
                "document_name": e.get("filename"),
                "document_url": e.get("source_url"),
                "document_type": (
                    "pricing_sheet"
                    if (
                        e.get("document_role_guess")
                        in {
                            "PRICING_SCHEDULE",
                            "LINE_ITEM_SCHEDULE",
                            "PRODUCT_SCHEDULE",
                            "ITEM_LIST",
                            "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES",
                            "SPECIFICATION_WITH_PRODUCT_TABLE",
                            "BID_FORM",
                        }
                        or int(e.get("extracted_line_count") or 0) >= 1
                    )
                    else e.get("document_role_guess")
                ),
                "document_role_content": e.get("document_role_content"),
                "content_hash": e.get("CONTENT_HASH"),
                "retrieval_status": e.get("retrieval_status") or "DOWNLOADED",
                "line_schedule_likely": int(e.get("extracted_line_count") or 0) >= 1
                or e.get("document_role_content") in product_roles,
            }
            for e in materialized
        ],
        "operator_message": content_recognition.get("operator_product_status")
        or operator_package_message(
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
