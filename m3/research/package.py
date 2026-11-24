"""Shared solicitation package acquisition engine (Build 002D).

ONE engine for automated research, classification recovery, saved opportunities,
manual URL/document imports, and future bid/amendment workflows.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, unquote

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.models import PackageDocument, SolicitationPackage
from m3.research.documents import (
    extract_csv_or_txt,
    extract_docx_text,
    extract_html_text,
    extract_pdf_text,
    extract_xlsx_text,
    find_attachment_urls,
    guess_format,
)
from m3.research.http_util import fetch_bytes, fetch_text, validate_public_https_url

PACKAGE_STATES = {
    "NOT_ACQUIRED",
    "ACQUISITION_IN_PROGRESS",
    "COMPLETE",
    "PARTIAL",
    "ACCESS_RESTRICTED",
    "VERIFICATION_REQUIRED",
    "FAILED",
    "UPDATE_AVAILABLE",
}

LISTING_URL_MARKERS = (
    "/bid-opportunities",
    "/bids/",
    "/current-bids",
    "/open-bids",
    "/solicitations",
    "/procurement",
    "/purchasing",
    "procuretap.com/bids/",
    "/commodity",
    "resource/",
)
DETAIL_URL_MARKERS = (
    "/solicitation/",
    "/opportunity/",
    "/bids/details/",
    "/bid/details/",
    "/bid/detail/",
    "/opportunity-details",
    "opportunity-details?",
    "/eventdetails",
    "eventdetails?",
    "esourcenumber=",
    "/bid/",
    "/rfp/",
    "/rfq/",
    "/ifb/",
    "/project/",
    "noticeid=",
    "solicitationid=",
    "opportunityid=",
    "getbiddocumentlist",
)
DOC_EXTS = (".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".doc", ".rtf", ".zip", ".png", ".jpg", ".jpeg")
LOGIN_MARKERS = (
    "sign in",
    "log in",
    "login",
    "authenticate",
    "vendor registration",
    "create an account",
    "access denied",
    "unauthorized",
)
# Salesforce Lightning / RAMP shells often lack classic login strings in the first bytes
DYNAMIC_PLATFORM_MARKERS = (
    "salesforce",
    "force.com",
    "lightning.force",
    "embeddedservice",
    "aura_prod",
    "opportunity-details",
    "visualforce",
)
AMENDMENT_MARKERS = ("amendment", "addendum", "addenda", "revision", "revised")
FALSE_COMPLETE_TITLE_RE = re.compile(
    r"(?i)^(glossary|rfp\s*vs\s*rfq|faq|home|welcome|privacy|login|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new\s*hampshire|"
    r"new\s*jersey|new\s*mexico|new\s*york|north\s*carolina|north\s*dakota|"
    r"ohio|oklahoma|oregon|pennsylvania|rhode\s*island|south\s*carolina|"
    r"south\s*dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west\s*virginia|wisconsin|wyoming|district\s*of\s*columbia|"
    r"commodity\s*codes?)$"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def _identity_key(item: dict[str, Any]) -> str:
    ek = (item.get("external_key") or "").strip()
    if ek:
        return f"ek:{ek}"[:240]
    sn = (item.get("solicitation_number") or "").strip()
    src = (item.get("source_key") or "").strip()
    if sn and src:
        return f"sn:{src}:{sn}"[:240]
    url = (item.get("official_url") or item.get("authoritative_url") or "").strip()
    if url:
        return f"url:{_sha256(url)[:32]}"
    title = (item.get("title") or "").strip()
    buyer = (item.get("buyer_name") or item.get("buyer") or "").strip()
    return f"tb:{_sha256(f'{buyer}|{title}')[:32]}"


_JUNK_HOST_MARKERS = (
    "fonts.gstatic.com",
    "fonts.googleapis.com",
    "google.com",
    "gstatic.com",
    "googleapis.com",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "youtube.com",
    "doubleclick.net",
    "googletagmanager.com",
    "cloudflare.com",
    "w3.org",
)


def _is_junk_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return True
    if any(host == j or host.endswith("." + j) for j in _JUNK_HOST_MARKERS):
        return True
    if host.endswith((".png", ".jpg", ".css", ".js")):
        return True
    path = urlparse(url).path.lower()
    if path.endswith((".css", ".js", ".woff", ".woff2", ".map", ".ico", ".svg")):
        return True
    return False


def classify_url_kind(url: str) -> str:
    if not url or not str(url).startswith("http"):
        return "missing"
    if _is_junk_url(url):
        return "invalid"
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in DOC_EXTS):
        return "document"
    low = url.lower()
    # Detail markers win over broad listing markers (e.g. /Bids/Details/N vs /bids/).
    if any(m in low for m in DETAIL_URL_MARKERS):
        return "detail"
    # Short path with id-like segment
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and re.search(r"\d{4,}|[a-f0-9-]{8,}", parts[-1], re.I):
        if not any(m in low for m in LISTING_URL_MARKERS):
            return "detail"
        # /Bids/Details/9401 style already caught; numeric tail under /bids/ is often detail
        if re.search(r"/bids?/details?/\d+", path):
            return "detail"
    if any(m in low for m in LISTING_URL_MARKERS):
        return "listing"
    if path in {"", "/"} or len(parts) <= 1:
        return "listing"
    return "unknown"


def detect_platform(url: str, source_key: str = "") -> str:
    host = (urlparse(url or "").hostname or "").lower()
    sk = (source_key or "").lower()
    if "rampla.org" in host or "la_ramp" in sk or "lacity" in sk and "ramp" in sk:
        return "la_ramp"
    if "mesaaz.gov" in host or "citydata.mesaaz.gov" in host or "mesa" in sk:
        return "mesa"
    if "socrata" in sk or "resource/" in (url or "") or host.startswith("data.") or "opendata" in host:
        return "socrata"
    if "procuretap" in host:
        return "procuretap"
    if "bonfirehub" in host or "euna" in host:
        return "euna_bonfire"
    if "opengov" in host:
        return "opengov"
    if "bidnet" in host:
        return "bidnet"
    if "sam.gov" in host:
        return "sam_gov"
    if "mmp.delaware.gov" in host:
        return "delaware_mmp"
    if "doas.state.ga.us" in host or "ssl.doas.state.ga.us" in host:
        return "georgia_gpr"
    if host.endswith(".gov") or host.endswith(".us"):
        return "gov_html"
    if "ionwave" in host:
        return "ionwave"
    if "planetbids" in host:
        return "planetbids"
    if "publicpurchase" in host:
        return "publicpurchase"
    return "generic_http"


def _looks_like_dynamic_platform_shell(html: str | bytes, url: str = "") -> bool:
    """True when page is a JS/Salesforce shell without usable solicitation body."""
    if isinstance(html, bytes):
        head = html[:12000].decode("utf-8", errors="ignore").lower()
    else:
        head = (html or "")[:12000].lower()
    hits = sum(1 for m in DYNAMIC_PLATFORM_MARKERS if m in head)
    if hits >= 2:
        return True
    host = (urlparse(url or "").hostname or "").lower()
    if "rampla.org" in host and ("opportunity-details" in (url or "").lower() or hits >= 1):
        # RAMP opportunity pages are Salesforce Lightning; thin static HTML ≠ package
        text_len = len(re.sub(r"<[^>]+>", " ", head))
        if text_len < 800 or "salesforce" in head or "force.com" in head:
            return True
    return False


def audit_false_complete_candidate(pkg_or_item: dict[str, Any] | Any) -> dict[str, Any]:
    """Explain why a COMPLETE package should be demoted (Build 002G)."""
    if hasattr(pkg_or_item, "title"):
        title = str(getattr(pkg_or_item, "title", "") or "")
        url_kind = str(getattr(pkg_or_item, "url_kind", "") or "")
        downloaded = int(getattr(pkg_or_item, "downloaded_count", 0) or 0)
        identified = int(getattr(pkg_or_item, "identified_count", 0) or 0)
        detail = getattr(pkg_or_item, "detail_fields", None) or {}
        auth = str(getattr(pkg_or_item, "authoritative_url", "") or "")
        status = str(getattr(pkg_or_item, "package_status", "") or "")
        identity = (detail or {}).get("record_type") or ""
    else:
        title = str(pkg_or_item.get("title") or "")
        url_kind = str(pkg_or_item.get("url_kind") or "")
        downloaded = int(pkg_or_item.get("downloaded_count") or 0)
        identified = int(pkg_or_item.get("identified_count") or 0)
        detail = pkg_or_item.get("detail_fields") or {}
        auth = str(pkg_or_item.get("authoritative_url") or "")
        status = str(pkg_or_item.get("package_status") or "")
        identity = str(pkg_or_item.get("record_type") or detail.get("record_type") or "")
    reasons: list[str] = []
    if status and status != "COMPLETE":
        return {"is_false_complete": False, "reasons": [], "recommended_status": status}
    if FALSE_COMPLETE_TITLE_RE.match(title.strip()):
        reasons.append("junk_or_non_solicitation_title")
    if downloaded == 0 and identified == 0 and url_kind != "detail":
        reasons.append("zero_documents_non_detail_url")
    if downloaded == 0 and identified == 0 and not detail.get("inventory_explicitly_empty"):
        if not detail.get("has_substantial_detail"):
            reasons.append("zero_documents_without_substantial_inline_requirement")
    if identity and identity != "INDIVIDUAL_SOLICITATION":
        reasons.append(f"record_type_not_individual:{identity}")
    if "rampla.org" in auth.lower() and downloaded == 0:
        reasons.append("ramp_dynamic_shell_without_documents")
    if reasons:
        return {
            "is_false_complete": True,
            "reasons": reasons,
            "recommended_status": "VERIFICATION_REQUIRED"
            if "zero_documents" in " ".join(reasons) or "junk" in " ".join(reasons)
            else "FAILED",
        }
    return {"is_false_complete": False, "reasons": [], "recommended_status": "COMPLETE"}


def packages_root() -> Path:
    root = get_settings().data_root() / "packages"
    root.mkdir(parents=True, exist_ok=True)
    return root


def content_store_root() -> Path:
    root = get_settings().data_root() / "package_blobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_path_for_hash(content_hash: str, ext: str = "") -> Path:
    sub = content_store_root() / content_hash[:2] / content_hash[2:4]
    sub.mkdir(parents=True, exist_ok=True)
    suffix = f".{ext.lstrip('.')}" if ext else ""
    return sub / f"{content_hash}{suffix}"


def _looks_like_login_html(data: bytes, ctype: str = "") -> bool:
    if "html" not in (ctype or "").lower() and not data[:200].lower().lstrip().startswith((b"<!doctype", b"<html")):
        # mislabeled pdf check
        if data.startswith(b"%PDF"):
            return False
        head = data[:4000].decode("utf-8", errors="ignore").lower()
        if not any(m in head for m in LOGIN_MARKERS):
            return False
    head = data[:8000].decode("utf-8", errors="ignore").lower()
    hits = sum(1 for m in LOGIN_MARKERS if m in head)
    return hits >= 2


def _is_empty_or_corrupt(data: bytes, expected_fmt: str) -> str | None:
    if not data:
        return "empty_file"
    if len(data) < 20:
        return "truncated_or_tiny"
    if expected_fmt == "pdf" and not data.startswith(b"%PDF"):
        if data.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<HTML")):
            return "html_returned_as_pdf"
        return "not_a_pdf"
    if expected_fmt == "docx" and not data[:2] == b"PK":
        return "not_a_docx_zip"
    if expected_fmt == "xlsx" and not data[:2] == b"PK":
        return "not_a_xlsx_zip"
    return None


def _sanitize_jsonable(obj: Any, *, depth: int = 0) -> Any:
    """Strip NUL / control garbage so Postgres JSONB never sees \\u0000."""
    if depth > 12:
        return None
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace").replace("\x00", "")[:8000]
    if isinstance(obj, str):
        # Reject raw PDF / binary mistaken for text
        if obj.startswith("%PDF") or "\x00" in obj:
            cleaned = obj.replace("\x00", "")
            if cleaned.lstrip().startswith("%PDF"):
                return "[binary_pdf_omitted_from_json]"
            return "".join(ch for ch in cleaned if ch in "\n\t" or ord(ch) >= 32)[:8000]
        return obj.replace("\x00", "")
    if isinstance(obj, list):
        return [_sanitize_jsonable(x, depth=depth + 1) for x in obj[:200]]
    if isinstance(obj, dict):
        return {str(k): _sanitize_jsonable(v, depth=depth + 1) for k, v in list(obj.items())[:200]}
    return str(obj)[:2000]


def extract_detail_fields(html: str, url: str) -> dict[str, Any]:
    raw = html or ""
    if raw.lstrip().startswith("%PDF") or "\x00" in raw[:200]:
        return {
            "detail_url": url,
            "description_excerpt": "",
            "binary_payload_omitted": True,
            "note": "url_returned_pdf_or_binary_not_html",
        }
    text = extract_html_text(raw)
    fields: dict[str, Any] = {"detail_url": url, "description_excerpt": text[:4000]}

    def _m(patterns: list[str]) -> str | None:
        for p in patterns:
            m = re.search(p, html, re.I | re.S)
            if m:
                val = re.sub(r"<[^>]+>", " ", m.group(1))
                val = re.sub(r"\s+", " ", val).strip()
                if val:
                    return val[:500]
        return None

    fields["solicitation_number"] = _m(
        [
            r"(?i)solicitation\s*(?:number|#|no\.?)[:\s]*</?(?:[^>]+)>?\s*([A-Z0-9][A-Z0-9\-_/]{3,})",
            r"(?i)(?:RFP|RFQ|IFB|RFB|ITB)\s*[#:]?\s*([A-Z0-9][A-Z0-9\-_/]{3,})",
            r"(?i)bid\s*(?:number|#|no\.?)[:\s]+([A-Z0-9][A-Z0-9\-_/]{3,})",
        ]
    )
    fields["closing"] = _m(
        [
            r"(?i)(?:closing|due|deadline|bid open(?:ing)?)[^:<]{0,40}[:\s]+([^<\n]{6,80})",
            r"(?i)(?:response due|proposals? due)[^:<]{0,40}[:\s]+([^<\n]{6,80})",
        ]
    )
    fields["contact"] = _m(
        [
            r"(?i)(?:buyer|purchasing\s+agent|contact)[^:<]{0,30}[:\s]+([^<\n]{4,120})",
            r"(?i)mailto:([^\"'\s>]+)",
        ]
    )
    fields["set_aside"] = _m([r"(?i)set[\s-]?aside[^:<]{0,20}[:\s]+([^<\n]{3,80})"])
    fields["procurement_method"] = _m(
        [r"(?i)(?:procurement\s+type|method|solicitation\s+type)[^:<]{0,20}[:\s]+([^<\n]{3,80})"]
    )
    # Amendments listed on page
    amend_links = []
    for u in find_attachment_urls(html, url):
        if any(m in u.lower() for m in AMENDMENT_MARKERS):
            amend_links.append(u)
    fields["amendment_urls"] = amend_links[:20]
    fields["full_text_len"] = len(text)
    fields["has_substantial_detail"] = len(text) >= 400
    return fields


def discover_delaware_mmp_documents(detail_url: str) -> list[dict[str, Any]]:
    """Delaware MMP loads attachments via GetBidDocumentList AJAX (public).

    Static detail HTML often has zero hrefs; documents live on bidcondocs.delaware.gov.
    """
    m = re.search(r"/Bids/Details/(\d+)", detail_url or "", flags=re.I)
    if not m:
        return []
    bid_id = m.group(1)
    list_url = f"https://mmp.delaware.gov/Bids/GetBidDocumentList?id={bid_id}&currentCount=0"
    try:
        status, final, html = fetch_text(list_url, timeout=15)
    except Exception:
        return []
    if status >= 400 or not html:
        return []
    if "Request Rejected" in html[:400]:
        return []
    return discover_attachments_from_html(html, final or list_url)


def discover_georgia_gpr_documents(detail_url: str, html: str = "") -> list[dict[str, Any]]:
    """Georgia Procurement Registry public attachment links on eventDetails pages."""
    if not html and detail_url:
        try:
            status, final, html = fetch_text(detail_url, timeout=15)
            if status >= 400:
                return []
            detail_url = final or detail_url
        except Exception:
            return []
    out: list[dict[str, Any]] = []
    for href, label in re.findall(
        r'<a[^>]+href=["\']([^"\']*downloadAttachment[^"\']*)["\'][^>]*>([\s\S]*?)</a>',
        html or "",
        flags=re.I,
    ):
        abs_u = urljoin(detail_url, href)
        name = re.sub(r"<[^>]+>", " ", label or "")
        name = re.sub(r"\s+", " ", name).strip() or unquote(urlparse(abs_u).path.split("/")[-1])
        is_am = any(m in name.lower() for m in AMENDMENT_MARKERS)
        out.append(
            {
                "source_url": abs_u,
                "original_filename": name[:500],
                "document_title": name[:500],
                "document_type": "amendment" if is_am else "attachment",
                "is_amendment": is_am,
                "meta": {"platform": "georgia_gpr"},
            }
        )
    return out


def discover_attachments_from_html(html: str, base_url: str) -> list[dict[str, Any]]:
    urls = find_attachment_urls(html, base_url)
    # Also catch data-download / onclick patterns lightly
    extra = re.findall(
        r'(?:href|data-url|data-href)=["\']([^"\']+\.(?:pdf|docx?|xlsx?|csv|zip|txt|rtf))["\']',
        html or "",
        flags=re.I,
    )
    for h in extra:
        abs_u = urljoin(base_url, h)
        if abs_u not in urls:
            urls.append(abs_u)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in urls[:40]:
        if u in seen:
            continue
        seen.add(u)
        name = unquote(urlparse(u).path.split("/")[-1] or "document")
        is_am = any(m in (u + name).lower() for m in AMENDMENT_MARKERS)
        out.append(
            {
                "source_url": u,
                "original_filename": name[:500],
                "document_title": name[:500],
                "document_type": "amendment" if is_am else "attachment",
                "is_amendment": is_am,
            }
        )
    return out


def resolve_authoritative_url(item: dict[str, Any]) -> dict[str, Any]:
    """Resolve solicitation-specific URL; reject generic listings without evidence."""
    url = (item.get("official_url") or item.get("authoritative_url") or "").strip()
    kind = classify_url_kind(url)
    platform = detect_platform(url, str(item.get("source_key") or ""))
    result = {
        "authoritative_url": url or None,
        "url_kind": kind,
        "platform": platform,
        "resolved": False,
        "reason": None,
    }
    if kind == "missing":
        result["reason"] = "missing_detail_url"
        return result
    if kind == "invalid":
        result["reason"] = "invalid_or_non_procurement_url"
        result["authoritative_url"] = None
        return result
    if kind == "document":
        result["resolved"] = True
        result["reason"] = "direct_document_url"
        return result
    if kind == "detail":
        result["resolved"] = True
        result["reason"] = "solicitation_detail_url"
        return result
    if kind == "listing":
        # Attempt lightweight resolution: search page for solicitation number / title link
        sn = (item.get("solicitation_number") or "").strip()
        title = (item.get("title") or "").strip()
        if not url.startswith("http"):
            result["reason"] = "listing_without_http"
            return result
        try:
            status, final, html = fetch_text(url, timeout=12)
            if status >= 400:
                result["reason"] = f"listing_http_{status}"
                return result
            if _looks_like_login_html(html.encode("utf-8", errors="ignore"), "text/html"):
                result["reason"] = "listing_requires_authentication"
                result["url_kind"] = "listing"
                return result
            base_host = (urlparse(final).hostname or "").lower()
            candidates = re.findall(r'href=["\']([^"\']+)["\']', html or "", flags=re.I)
            best = None
            sn_l = sn.lower()
            title_tok = re.findall(r"[a-z0-9]{4,}", title.lower())[:6]
            for href in candidates:
                abs_u = urljoin(final, href)
                if _is_junk_url(abs_u):
                    continue
                cand_host = (urlparse(abs_u).hostname or "").lower()
                # Prefer same-site or .gov/.us; never jump to CDNs
                if cand_host and base_host and cand_host != base_host:
                    if not (
                        cand_host.endswith(".gov")
                        or cand_host.endswith(".us")
                        or cand_host.endswith("." + ".".join(base_host.split(".")[-2:]))
                    ):
                        continue
                low = abs_u.lower()
                if abs_u == url or abs_u.rstrip("/") == url.rstrip("/"):
                    continue
                score = 0
                if sn_l and sn_l in low:
                    score += 5
                if sn_l and sn_l in href.lower():
                    score += 5
                if any(t in low for t in title_tok):
                    score += 1
                if classify_url_kind(abs_u) == "detail":
                    score += 3
                if any(low.split("?")[0].endswith(ext) for ext in DOC_EXTS):
                    score += 2
                if score >= 5:
                    best = abs_u
                    break
                if score >= 3 and not best:
                    best = abs_u
            if best:
                result["authoritative_url"] = best
                result["url_kind"] = classify_url_kind(best)
                result["resolved"] = True
                result["reason"] = "resolved_from_listing"
                return result
            result["reason"] = "listing_no_solicitation_match"
            return result
        except Exception as exc:
            result["reason"] = f"listing_resolve_error:{type(exc).__name__}"
            return result
    # unknown: attempt fetch but do not claim resolved authority without detail markers
    result["resolved"] = False
    result["reason"] = "unknown_url_kind_attempt"
    return result


def _safe_extract_zip(data: bytes, dest: Path, *, max_files: int = 40, max_total: int = 40_000_000) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for i, info in enumerate(zf.infolist()):
            if i >= max_files:
                break
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if ".." in name or name.startswith("/"):
                continue
            if total + info.file_size > max_total:
                break
            target = dest / Path(name).name
            with zf.open(info) as src, open(target, "wb") as out:
                chunk = src.read(max_total - total)
                out.write(chunk)
                total += len(chunk)
            written.append(target)
    return written


def extract_document_bytes(data: bytes, fmt: str) -> dict[str, Any]:
    from m3.research.document_quality import classify_document_text, classify_raw_bytes

    # Reject portal/login HTML before counting as a solicitation document
    pre = classify_raw_bytes(data, fmt=fmt)
    if pre.get("is_junk") and fmt in {"html", "unknown", ""}:
        return {
            "ok": False,
            "text": "",
            "format": fmt or "html",
            "reason": pre.get("reason") or "junk_portal_or_error_page",
            "is_junk": True,
        }

    if fmt == "pdf":
        result = extract_pdf_text(data)
    elif fmt == "docx":
        result = extract_docx_text(data)
    elif fmt == "xlsx":
        result = extract_xlsx_text(data)
    elif fmt in {"text", "csv", "txt"}:
        result = extract_csv_or_txt(data)
    elif fmt == "html":
        text = extract_html_text(data.decode("utf-8", errors="replace"))
        result = {
            "ok": len(text) >= 40,
            "text": text,
            "format": "html",
            "reason": None if len(text) >= 40 else "html_too_thin",
        }
    elif fmt == "zip":
        return {"ok": False, "reason": "zip_preserved_not_flattened", "text": "", "format": "zip"}
    else:
        return {"ok": False, "reason": f"unsupported_extraction:{fmt}", "text": "", "format": fmt}

    if result.get("ok") and result.get("text"):
        quality = classify_document_text(str(result.get("text") or ""), fmt=fmt)
        if quality.get("is_junk"):
            return {
                "ok": False,
                "text": "",
                "format": fmt,
                "reason": quality.get("reason") or "junk_portal_content",
                "is_junk": True,
            }
        result["is_solicitation_body"] = quality.get("is_solicitation_body")
    return result


def compute_package_status(
    *,
    url_kind: str,
    resolved: bool,
    identified: int,
    downloaded: int,
    access_issues: list[str],
    detail_ok: bool,
    inventory_verified: bool,
    has_substantial_detail: bool,
    verified_individual_solicitation: bool = True,
    inventory_explicitly_empty: bool = False,
    title: str = "",
) -> str:
    """Compute package status. COMPLETE requires verified solicitation identity.

    Inline-only COMPLETE is allowed only for verified individual solicitations on
    detail pages where the attachment inventory was examined and is empty, with
    substantial on-page requirements — never for generic/junk pages.
    """
    auth_blockers = {
        "authentication_required",
        "registration_required",
        "access_restricted",
        "listing_requires_authentication",
        "dynamic_platform_shell",
        "ramp_dynamic_content_requires_auth",
    }
    if any(a in auth_blockers or a.startswith("auth") or "dynamic_platform" in a for a in access_issues):
        if downloaded == 0:
            return "ACCESS_RESTRICTED"
    if not verified_individual_solicitation:
        if any("access_restricted" in a or "authentication" in a for a in access_issues):
            return "ACCESS_RESTRICTED"
        if url_kind in {"listing", "unknown", "missing"}:
            return "VERIFICATION_REQUIRED"
        if downloaded > 0:
            return "PARTIAL"
        return "FAILED"
    if title and FALSE_COMPLETE_TITLE_RE.match(title.strip()):
        return "VERIFICATION_REQUIRED"
    if not resolved and url_kind in {"listing", "missing", "invalid", "unknown"}:
        if url_kind == "listing":
            return "VERIFICATION_REQUIRED"
        if url_kind == "invalid":
            return "FAILED"
        if url_kind == "unknown" and detail_ok and has_substantial_detail:
            return "VERIFICATION_REQUIRED"
        if url_kind == "unknown":
            return "FAILED"
        return "FAILED"
    if identified == 0:
        # COMPLETE with zero files only when this is a solicitation detail page,
        # inventory was examined and confirmed empty, and substantial requirement text is on-page.
        if (
            detail_ok
            and has_substantial_detail
            and inventory_verified
            and inventory_explicitly_empty
            and url_kind == "detail"
            and resolved
            and verified_individual_solicitation
        ):
            return "COMPLETE"
        if detail_ok and has_substantial_detail:
            return "VERIFICATION_REQUIRED"
        if detail_ok:
            return "PARTIAL"
        return "FAILED"
    if downloaded >= identified and identified > 0 and inventory_verified:
        return "COMPLETE"
    if downloaded > 0:
        return "PARTIAL"
    if access_issues:
        return "ACCESS_RESTRICTED" if any(
            "auth" in a or "regist" in a or "dynamic_platform" in a or "access_restricted" in a
            for a in access_issues
        ) else "PARTIAL"
    return "FAILED"


def acquire_solicitation_package(
    db: Session,
    item: dict[str, Any],
    *,
    opportunity_id=None,
    research_run_id=None,
    manual_urls: list[str] | None = None,
    uploaded_files: list[dict[str, Any]] | None = None,
    refresh: bool = False,
    max_documents: int = 200,
    mode: str = "full",
) -> SolicitationPackage:
    """Acquire/update one solicitation package. Shared entry point for all workflows.

    mode:
      - full: complete package acquisition
      - targeted: detail + at most one document for classification (002F)
      - skip_mark: record disposition without network work

    max_documents is an operational safeguard; overflow is recorded as PARTIAL with
    remaining URLs listed in detail_fields — not permanently abandoned.
    """
    from m3.research.perf import timed

    if mode == "targeted":
        max_documents = min(max_documents, 1)

    with timed("acquire_package_total"):
        return _acquire_solicitation_package_inner(
            db,
            item,
            opportunity_id=opportunity_id,
            research_run_id=research_run_id,
            manual_urls=manual_urls,
            uploaded_files=uploaded_files,
            refresh=refresh,
            max_documents=max_documents,
            mode=mode,
        )


def _acquire_solicitation_package_inner(
    db: Session,
    item: dict[str, Any],
    *,
    opportunity_id=None,
    research_run_id=None,
    manual_urls: list[str] | None = None,
    uploaded_files: list[dict[str, Any]] | None = None,
    refresh: bool = False,
    max_documents: int = 200,
    mode: str = "full",
) -> SolicitationPackage:
    from m3.research.perf import timed

    ikey = _identity_key(item)
    pkg = db.query(SolicitationPackage).filter_by(identity_key=ikey).one_or_none()
    if not pkg:
        pkg = SolicitationPackage(identity_key=ikey)
        db.add(pkg)
    # Incremental reuse: skip network if already complete/partial and not refresh
    if (
        not refresh
        and mode == "full"
        and pkg.package_status in {"COMPLETE", "PARTIAL", "ACCESS_RESTRICTED"}
        and pkg.last_verified_at
        and (pkg.downloaded_count or 0) >= 0
    ):
        # Demote previously false COMPLETE packages (Build 002G integrity)
        audit = audit_false_complete_candidate(pkg)
        if audit.get("is_false_complete") and pkg.package_status == "COMPLETE":
            pkg.package_status = audit["recommended_status"]
            pkg.detail_fields = {
                **(pkg.detail_fields or {}),
                "false_complete_demoted": True,
                "false_complete_reasons": audit.get("reasons") or [],
            }
            db.add(pkg)
            db.commit()
            db.refresh(pkg)
            # Fall through to re-acquire when demoted from COMPLETE
        elif pkg.downloaded_count or pkg.package_status in {"COMPLETE", "PARTIAL", "ACCESS_RESTRICTED"}:
            pkg.detail_fields = {
                **(pkg.detail_fields or {}),
                "incremental_reuse": True,
                "reuse_mode": mode,
            }
            db.add(pkg)
            db.commit()
            db.refresh(pkg)
            return pkg

    if mode == "skip_mark":
        pkg.title = str(item.get("title") or pkg.title or "")[:2000]
        pkg.buyer = str(item.get("buyer_name") or item.get("buyer") or pkg.buyer or "")[:255]
        pkg.source_key = str(item.get("source_key") or pkg.source_key or "")[:128]
        pkg.external_key = item.get("external_key") or pkg.external_key
        pkg.package_status = "NOT_ACQUIRED"
        pkg.access_issues = list(pkg.access_issues or []) + [
            f"skipped_full_acquisition:{(item.get('_skip_reason') or 'qualified_out')}"
        ]
        pkg.detail_fields = {
            **(pkg.detail_fields or {}),
            "acquisition_disposition": item.get("_qualification") or {},
            "skipped_full_package": True,
        }
        pkg.last_verified_at = _now()
        db.add(pkg)
        db.commit()
        db.refresh(pkg)
        return pkg

    pkg.package_status = "ACQUISITION_IN_PROGRESS"
    pkg.title = str(item.get("title") or pkg.title or "")[:2000]
    pkg.buyer = str(item.get("buyer_name") or item.get("buyer") or pkg.buyer or "")[:255]
    sn_raw = item.get("solicitation_number") or pkg.solicitation_number or None
    if sn_raw is not None:
        sn_raw = str(sn_raw).strip()[:128] or None
    pkg.solicitation_number = sn_raw
    pkg.source_key = str(item.get("source_key") or pkg.source_key or "")[:128]
    pkg.external_key = item.get("external_key") or pkg.external_key
    pkg.opportunity_id = opportunity_id or pkg.opportunity_id
    pkg.research_run_id = research_run_id or pkg.research_run_id
    pkg.access_issues = []
    pkg.error = None
    db.flush()

    with timed("url_resolution"):
        resolution = resolve_authoritative_url(item)
    pkg.authoritative_url = resolution.get("authoritative_url")
    pkg.url_kind = resolution.get("url_kind") or "unknown"
    pkg.platform = resolution.get("platform") or detect_platform(pkg.authoritative_url or "", pkg.source_key)
    access_issues: list[str] = []
    if resolution.get("reason") and not resolution.get("resolved"):
        access_issues.append(str(resolution["reason"]))

    identified_docs: list[dict[str, Any]] = []
    detail_fields: dict[str, Any] = {"acquisition_mode": mode}
    detail_ok = False
    inventory_verified = False
    has_substantial = False

    # Manual supplements
    for u in manual_urls or []:
        if u and str(u).startswith("http"):
            identified_docs.append(
                {
                    "source_url": u,
                    "original_filename": unquote(urlparse(u).path.split("/")[-1] or "manual"),
                    "document_title": "manual_import",
                    "document_type": "manual_upload_ref",
                    "is_amendment": False,
                    "meta": {"provenance": "manual_url"},
                }
            )

    auth_url = pkg.authoritative_url
    if auth_url and pkg.url_kind == "document":
        identified_docs.append(
            {
                "source_url": auth_url,
                "original_filename": unquote(urlparse(auth_url).path.split("/")[-1] or "solicitation"),
                "document_title": "primary_document",
                "document_type": "solicitation",
                "is_amendment": False,
            }
        )
        detail_ok = True
        inventory_verified = True  # single known document URL
        has_substantial = True
    elif auth_url and pkg.url_kind != "invalid" and (
        resolution.get("resolved") or pkg.url_kind in {"detail", "unknown", "listing"}
    ):
        try:
            status, final, html = fetch_text(auth_url, timeout=15)
            if status in (401, 403):
                access_issues.append("authentication_required" if status == 401 else "access_restricted")
            elif status >= 400:
                access_issues.append(f"detail_http_{status}")
            elif _looks_like_login_html(html.encode("utf-8", errors="ignore"), "text/html"):
                access_issues.append("authentication_required")
            elif _looks_like_dynamic_platform_shell(html, final):
                access_issues.append("dynamic_platform_shell")
                if "rampla.org" in (final or "").lower():
                    access_issues.append("ramp_dynamic_content_requires_auth")
                detail_fields = {
                    "detail_url": final,
                    "dynamic_shell": True,
                    "note": "Platform returns interactive shell; documents not publicly extractable from static HTML",
                }
                pkg.authoritative_url = final
            else:
                detail_fields = extract_detail_fields(html, final)
                detail_ok = True
                has_substantial = bool(detail_fields.get("has_substantial_detail"))
                pkg.authoritative_url = final
                if detail_fields.get("solicitation_number") and not pkg.solicitation_number:
                    pkg.solicitation_number = str(detail_fields["solicitation_number"]).strip()[:128]
                discovered = discover_attachments_from_html(html, final)
                # Delaware MMP: attachments are AJAX-loaded, not in static detail HTML
                if (pkg.platform == "delaware_mmp" or "mmp.delaware.gov" in (final or "").lower()) and not any(
                    (d.get("source_url") or "").lower().endswith(DOC_EXTS) for d in discovered
                ):
                    de_docs = discover_delaware_mmp_documents(final)
                    if de_docs:
                        discovered.extend(de_docs)
                        detail_fields["delaware_mmp_document_list"] = True
                        detail_fields["delaware_mmp_document_list_count"] = len(de_docs)
                # Georgia GPR: downloadAttachment links on eventDetails (public PDFs)
                if pkg.platform == "georgia_gpr" or "doas.state.ga.us" in (final or "").lower():
                    ga_docs = discover_georgia_gpr_documents(final, html)
                    if ga_docs:
                        # Prefer explicit attachment endpoints over generic href harvest
                        discovered.extend(ga_docs)
                        detail_fields["georgia_gpr_document_list"] = True
                        detail_fields["georgia_gpr_document_list_count"] = len(ga_docs)
                identified_docs.extend(discovered)
                # Inventory is only verified on solicitation detail pages (not generic listings)
                effective_kind = classify_url_kind(final)
                pkg.url_kind = effective_kind if effective_kind != "missing" else pkg.url_kind
                inventory_verified = effective_kind == "detail"
                detail_fields["attachment_inventory_examined"] = inventory_verified
                detail_fields["attachment_inventory_count"] = len(discovered)
                detail_fields["inventory_explicitly_empty"] = bool(
                    inventory_verified and len(discovered) == 0
                )
                if effective_kind == "listing":
                    access_issues.append("detail_page_is_listing_inventory_unverified")
                # NJ / multi-notice PDF boundary detection when page links a single PDF later
                if discovered and any((d.get("source_url") or "").lower().endswith(".pdf") for d in discovered):
                    detail_fields["multi_notice_check_pending"] = True
        except Exception as exc:
            access_issues.append(f"detail_fetch_error:{type(exc).__name__}")

    # Dedupe identified by URL
    by_url: dict[str, dict] = {}
    for d in identified_docs:
        by_url[d["source_url"]] = d
    all_identified = list(by_url.values())
    overflow_docs = all_identified[max_documents:]
    identified_docs = all_identified[:max_documents]
    if overflow_docs:
        detail_fields = dict(detail_fields or {})
        detail_fields["document_overflow_count"] = len(overflow_docs)
        detail_fields["document_overflow_urls"] = [d["source_url"] for d in overflow_docs[:50]]
        access_issues.append(f"document_cap_partial:{len(overflow_docs)}_remaining")
        inventory_verified = False  # cannot claim complete while capped remainder exists

    # Persist / update document rows
    existing = {d.source_url_hash: d for d in (pkg.documents or [])}
    downloaded = 0
    extracted = 0
    storage_bytes = 0
    amendment_urls: list[str] = list(detail_fields.get("amendment_urls") or [])

    for spec in identified_docs:
        url = spec["source_url"]
        uh = _sha256(url)
        doc = existing.get(uh)
        if not doc:
            doc = PackageDocument(package_id=pkg.id, source_url=url, source_url_hash=uh)
            db.add(doc)
            existing[uh] = doc
        doc.original_filename = spec.get("original_filename") or doc.original_filename or ""
        doc.document_title = spec.get("document_title") or doc.document_title or ""
        doc.document_type = spec.get("document_type") or doc.document_type or "attachment"
        doc.is_amendment = bool(spec.get("is_amendment"))
        doc.meta = {**(doc.meta or {}), **(spec.get("meta") or {})}
        if doc.download_status == "downloaded" and doc.content_hash and not refresh:
            downloaded += 1
            storage_bytes += int(doc.file_size or 0)
            if doc.extraction_status == "extracted":
                extracted += 1
            continue
        # Download
        try:
            validate_public_https_url(url)
            status, final, data, ctype = fetch_bytes(url, timeout=20)
            if status in (401, 403):
                doc.download_status = "failed"
                doc.failure_reason = "authentication_required" if status == 401 else "access_restricted"
                access_issues.append(doc.failure_reason)
                continue
            if status >= 400:
                doc.download_status = "failed"
                doc.failure_reason = f"http_{status}"
                continue
            if _looks_like_login_html(data, ctype):
                doc.download_status = "failed"
                doc.failure_reason = "authentication_page_returned"
                access_issues.append("authentication_required")
                continue
            fmt = guess_format(final, ctype, data[:64])
            bad = _is_empty_or_corrupt(data, fmt if fmt != "unknown" else "")
            if bad:
                doc.download_status = "failed"
                doc.failure_reason = bad
                continue
            # Portal / employee / error HTML is not a solicitation package document
            from m3.research.document_quality import classify_raw_bytes

            raw_q = classify_raw_bytes(data, ctype=ctype or "", fmt=fmt)
            if raw_q.get("is_junk"):
                doc.download_status = "failed"
                doc.failure_reason = f"junk_content:{raw_q.get('reason')}"
                doc.file_format = fmt
                access_issues.append("junk_portal_or_non_solicitation_page")
                continue
            ch = _sha256(data)
            ext = fmt if fmt not in {"unknown", "html"} else (Path(urlparse(final).path).suffix.lstrip(".") or "bin")
            path = blob_path_for_hash(ch, ext)
            if not path.exists():
                path.write_bytes(data)
            doc.content_hash = ch
            doc.storage_path = str(path)
            doc.file_size = len(data)
            doc.file_format = fmt
            doc.download_status = "downloaded"
            doc.retrieved_at = _now()
            doc.failure_reason = None
            downloaded += 1
            storage_bytes += len(data)

            # Safe zip expand (nested files as additional identified docs — not auto-trusted complete)
            if fmt == "zip":
                zdir = packages_root() / str(pkg.id) / "zip" / ch[:12]
                try:
                    _safe_extract_zip(data, zdir)
                    doc.meta = {**(doc.meta or {}), "zip_extracted_to": str(zdir)}
                except Exception as zexc:
                    doc.meta = {**(doc.meta or {}), "zip_error": type(zexc).__name__}

            # Extract
            ex = extract_document_bytes(data, fmt)
            if ex.get("ok"):
                doc.extraction_status = "extracted"
                doc.extracted_excerpt = (ex.get("text") or "")[:8000]
                extracted += 1
            else:
                doc.extraction_status = "failed" if ex.get("reason") else "unsupported"
                doc.failure_reason = (doc.failure_reason or "") + (f";extract:{ex.get('reason')}" if ex.get("reason") else "")
                if ex.get("is_junk"):
                    # Do not treat junk portal HTML as a successful document acquisition
                    doc.download_status = "failed"
                    doc.failure_reason = f"junk_content:{ex.get('reason')}"
                    access_issues.append("junk_portal_or_non_solicitation_page")
                    downloaded = max(0, downloaded - 1)
                    storage_bytes = max(0, storage_bytes - int(doc.file_size or 0))
        except Exception as exc:
            doc.download_status = "failed"
            doc.failure_reason = f"download_error:{type(exc).__name__}"

    # Manual uploaded bytes
    for up in uploaded_files or []:
        data = up.get("data") or b""
        if not data:
            continue
        name = str(up.get("filename") or "upload.bin")
        fake_url = f"manual://upload/{pkg.id}/{name}"
        uh = _sha256(fake_url)
        doc = existing.get(uh)
        if not doc:
            doc = PackageDocument(package_id=pkg.id, source_url=fake_url, source_url_hash=uh)
            db.add(doc)
        doc.original_filename = name
        doc.document_title = str(up.get("title") or name)
        doc.document_type = "manual_upload"
        doc.meta = {"provenance": "manual_upload", "note": "does_not_establish_authoritative_completeness"}
        ch = _sha256(data)
        fmt = guess_format(name, up.get("content_type") or "", data[:64])
        path = blob_path_for_hash(ch, fmt if fmt != "unknown" else "bin")
        if not path.exists():
            path.write_bytes(data)
        doc.content_hash = ch
        doc.storage_path = str(path)
        doc.file_size = len(data)
        doc.file_format = fmt
        doc.download_status = "downloaded"
        doc.retrieved_at = _now()
        identified_docs.append({"source_url": fake_url})
        downloaded += 1
        storage_bytes += len(data)
        ex = extract_document_bytes(data, fmt)
        if ex.get("ok"):
            doc.extraction_status = "extracted"
            doc.extracted_excerpt = (ex.get("text") or "")[:8000]
            extracted += 1
        else:
            doc.extraction_status = "unsupported"
        # Manual upload never alone proves inventory completeness
        inventory_verified = False
        access_issues.append("manual_upload_supplement_only")

    identified_count = len(identified_docs)
    missing = max(0, identified_count - downloaded)
    pkg.identified_count = identified_count
    pkg.downloaded_count = downloaded
    pkg.missing_count = missing
    pkg.extracted_count = extracted
    pkg.storage_bytes = storage_bytes
    pkg.detail_fields = _sanitize_jsonable(detail_fields)
    pkg.access_issues = list(dict.fromkeys(access_issues))
    pkg.amendment_summary = _sanitize_jsonable(
        {
            "amendment_urls_on_page": amendment_urls,
            "amendment_documents": sum(1 for d in (pkg.documents or []) if d.is_amendment),
            "note": "Supersession only when amendment language or authoritative evidence present",
        }
    )
    # Multi-notice PDF: record boundaries from extracted text (do not invent records)
    try:
        from m3.research.identity import split_multi_notice_text

        for d in pkg.documents or []:
            if d.extracted_excerpt and (d.file_format == "pdf" or (d.source_url or "").lower().endswith(".pdf")):
                notices = split_multi_notice_text(d.extracted_excerpt)
                if len(notices) >= 2:
                    detail_fields = dict(detail_fields or {})
                    detail_fields["multi_notice_boundaries"] = [
                        {"solicitation_number": n.get("solicitation_number"), "evidence": n.get("evidence")}
                        for n in notices[:20]
                    ]
                    detail_fields["multi_notice_count"] = len(notices)
                    access_issues.append("multi_notice_document_requires_boundary_split")
                    inventory_verified = False
                    break
    except Exception:
        pass
    pkg.detail_fields = _sanitize_jsonable(detail_fields)

    # Identity gate for COMPLETE (prefer item fields; fall back to URL/doc evidence)
    verified_sol = bool(item.get("verified_individual_solicitation"))
    if not verified_sol:
        from m3.research.identity import verify_record_identity

        ident = verify_record_identity(item)
        verified_sol = bool(ident.get("verified_individual_solicitation"))
        detail_fields = dict(detail_fields or {})
        detail_fields["record_type"] = ident.get("record_type")
        detail_fields["record_type_evidence"] = ident.get("record_type_evidence")
        pkg.detail_fields = detail_fields
    # Document URL alone can verify individual solicitation for package purposes
    if not verified_sol and pkg.url_kind == "document" and downloaded > 0:
        verified_sol = True
        detail_fields = dict(detail_fields or {})
        detail_fields["record_type"] = detail_fields.get("record_type") or "INDIVIDUAL_SOLICITATION"
        detail_fields["identity_strength"] = "downloaded_document"
        pkg.detail_fields = detail_fields

    pkg.package_status = compute_package_status(
        url_kind=pkg.url_kind,
        resolved=bool(resolution.get("resolved")),
        identified=identified_count,
        downloaded=downloaded,
        access_issues=list(dict.fromkeys(list(pkg.access_issues or []) + access_issues)),
        detail_ok=detail_ok,
        inventory_verified=inventory_verified and "manual_upload_supplement_only" not in (pkg.access_issues or []),
        has_substantial_detail=has_substantial,
        verified_individual_solicitation=verified_sol,
        inventory_explicitly_empty=bool((detail_fields or {}).get("inventory_explicitly_empty")),
        title=pkg.title or "",
    )
    pkg.access_issues = list(dict.fromkeys(list(pkg.access_issues or []) + access_issues))
    if pkg.package_status == "COMPLETE" and extracted < downloaded:
        pkg.extraction_status = "PARTIAL"
    elif extracted and extracted >= downloaded and downloaded > 0:
        pkg.extraction_status = "READY"
    elif downloaded and extracted == 0:
        pkg.extraction_status = "FAILED"
    elif detail_ok and identified_count == 0:
        pkg.extraction_status = "DETAIL_ONLY"
    else:
        pkg.extraction_status = "NOT_READY"

    # Classification + quantity extraction from package (Build 002H)
    excerpts = []
    if detail_fields.get("description_excerpt"):
        excerpts.append(str(detail_fields["description_excerpt"])[:3000])
    for d in pkg.documents or []:
        if d.extracted_excerpt:
            excerpts.append(d.extracted_excerpt[:2000])
    evidence_text = "\n".join(excerpts)[:12000]
    if evidence_text:
        from m3.research.classify import classify_requirement
        from m3.research.extract_fields import extract_identity_fields, extract_quantity_fields

        cls = classify_requirement(
            pkg.title,
            category_raw=str(item.get("product_category") or ""),
            description=str(item.get("description") or "")[:2000],
            document_text=evidence_text,
        )
        qty = extract_quantity_fields(evidence_text, title=pkg.title or "")
        ident = extract_identity_fields(evidence_text)
        pkg.classification_hint = _sanitize_jsonable(
            {
                "requirement_class": cls.get("requirement_class"),
                "include_in_product_resale": cls.get("include_in_product_resale"),
                "evidence": (cls.get("classification_evidence") or [])[:8],
                "from_package": True,
                "quantity": qty.get("quantity"),
                "quantity_unit": qty.get("quantity_unit"),
                "quantity_kind": qty.get("quantity_kind"),
                "quantity_evidence": (qty.get("quantity_evidence") or [])[:6],
                "quantity_extracted": bool(qty.get("quantity_extracted")),
                "line_items": (qty.get("line_items") or [])[:10],
                "identity_fields": ident,
                "description_excerpt": (detail_fields.get("description_excerpt") or "")[:2000],
            }
        )
        detail_fields = dict(detail_fields or {})
        if qty.get("quantity_extracted"):
            detail_fields["extracted_quantity"] = qty.get("quantity")
            detail_fields["extracted_quantity_kind"] = qty.get("quantity_kind")
            detail_fields["extracted_quantity_evidence"] = (qty.get("quantity_evidence") or [])[:4]
        pkg.detail_fields = _sanitize_jsonable(detail_fields)
    pkg.last_verified_at = _now()
    db.add(pkg)
    # Persist reusable platform success pattern (avoid rediscovering with OpenAI)
    if pkg.package_status in {"COMPLETE", "PARTIAL"} and pkg.platform and pkg.authoritative_url:
        try:
            from m3.models import ResearchCache

            ck = f"platform_pattern:{pkg.platform}:{urlparse(pkg.authoritative_url).hostname or ''}"
            row = db.query(ResearchCache).filter_by(cache_key=ck).one_or_none()
            payload = {
                "platform": pkg.platform,
                "url_kind": pkg.url_kind,
                "host": urlparse(pkg.authoritative_url).hostname,
                "sample_url": pkg.authoritative_url[:500],
                "identified": pkg.identified_count,
                "downloaded": pkg.downloaded_count,
                "status": pkg.package_status,
            }
            if not row:
                row = ResearchCache(cache_key=ck, kind="platform_acquisition_pattern", payload=payload)
            else:
                row.payload = payload
                row.hit_count = int(row.hit_count or 0) + 1
            db.add(row)
        except Exception:
            pass
    db.commit()
    db.refresh(pkg)
    return pkg


def package_to_item_enrichment(pkg: SolicitationPackage) -> dict[str, Any]:
    """Merge package outcomes into a discovery/research item dict."""
    out: dict[str, Any] = {
        "package_id": str(pkg.id),
        "package_status": pkg.package_status,
        "package_identified": pkg.identified_count,
        "package_downloaded": pkg.downloaded_count,
        "package_missing": pkg.missing_count,
        "package_extracted": pkg.extracted_count,
        "authoritative_url": pkg.authoritative_url,
        "url_kind": pkg.url_kind,
        "platform": pkg.platform,
        "package_access_issues": list(pkg.access_issues or []),
        "extraction_status": pkg.extraction_status,
    }
    hint = pkg.classification_hint or {}
    if hint.get("requirement_class"):
        out["requirement_class"] = hint["requirement_class"]
        out["include_in_product_resale"] = hint.get("include_in_product_resale")
        out["classification_evidence"] = list(hint.get("evidence") or [])
        out["uncertain_resolution"] = (
            hint["requirement_class"]
            if hint["requirement_class"] not in {"unknown_mixed", "unknown"}
            else "requires_further_verification"
        )
        out["recovery_level"] = max(int(out.get("recovery_level") or 0), 3)
        out["recovery_via"] = "package_acquisition"
    if pkg.detail_fields:
        df = pkg.detail_fields
        if df.get("solicitation_number"):
            out["solicitation_number"] = df["solicitation_number"]
        if df.get("description_excerpt") and len(df["description_excerpt"]) > 80:
            out["description"] = df["description_excerpt"][:4000]
    return out


def acquire_packages_for_items(
    db: Session,
    items: list[dict[str, Any]],
    *,
    research_run_id=None,
    max_items: int | None = None,
    priority_uncertain: bool = True,
) -> dict[str, Any]:
    """Batch acquire packages; one inaccessible source must not stall the job."""
    work = list(items)
    if priority_uncertain:
        def _score(it: dict) -> int:
            req = (it.get("requirement_class") or "").lower()
            if req in {"unknown_mixed", "unknown", "pending_classification", ""}:
                return 0
            if not it.get("official_url"):
                return 1
            return 2

        work.sort(key=_score)
    if max_items is not None:
        work = work[:max_items]

    stats = {
        "total": len(work),
        "COMPLETE": 0,
        "PARTIAL": 0,
        "ACCESS_RESTRICTED": 0,
        "VERIFICATION_REQUIRED": 0,
        "FAILED": 0,
        "UPDATE_AVAILABLE": 0,
        "ACQUISITION_IN_PROGRESS": 0,
        "NOT_ACQUIRED": 0,
        "errors": 0,
        "newly_product": 0,
        "newly_resolved": 0,
        "identified_docs": 0,
        "downloaded_docs": 0,
        "by_source": {},
        "by_platform": {},
        "packages": [],
    }
    enriched: list[dict[str, Any]] = []
    for it in work:
        before = (it.get("requirement_class") or "").lower()
        try:
            pkg = acquire_solicitation_package(db, it, research_run_id=research_run_id)
            enrich = package_to_item_enrichment(pkg)
            merged = {**it, **enrich}
            after = (merged.get("requirement_class") or "").lower()
            from m3.research.classify import PRODUCT_CLASSES

            if before in {"unknown_mixed", "unknown", "pending_classification", ""} and after not in {
                "unknown_mixed",
                "unknown",
                "pending_classification",
                "",
            }:
                stats["newly_resolved"] += 1
            if after in PRODUCT_CLASSES and before not in PRODUCT_CLASSES:
                stats["newly_product"] += 1
            st = pkg.package_status
            stats[st] = stats.get(st, 0) + 1
            stats["identified_docs"] += pkg.identified_count
            stats["downloaded_docs"] += pkg.downloaded_count
            sk = pkg.source_key or "unknown"
            pl = pkg.platform or "unknown"
            stats["by_source"].setdefault(sk, {"n": 0, "downloaded": 0, "complete": 0})
            stats["by_source"][sk]["n"] += 1
            stats["by_source"][sk]["downloaded"] += pkg.downloaded_count
            if st == "COMPLETE":
                stats["by_source"][sk]["complete"] += 1
            stats["by_platform"].setdefault(pl, {"n": 0, "downloaded": 0, "complete": 0})
            stats["by_platform"][pl]["n"] += 1
            stats["by_platform"][pl]["downloaded"] += pkg.downloaded_count
            if st == "COMPLETE":
                stats["by_platform"][pl]["complete"] += 1
            stats["packages"].append(
                {
                    "id": str(pkg.id),
                    "title": (pkg.title or "")[:120],
                    "status": st,
                    "identified": pkg.identified_count,
                    "downloaded": pkg.downloaded_count,
                    "url": pkg.authoritative_url,
                    "platform": pkg.platform,
                    "source_key": pkg.source_key,
                }
            )
            enriched.append(merged)
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            stats["errors"] += 1
            stats["FAILED"] = stats.get("FAILED", 0) + 1
            merged = dict(it)
            merged["package_status"] = "FAILED"
            merged["package_error"] = type(exc).__name__
            enriched.append(merged)
    stats["items"] = enriched
    return stats


def storage_consumption_report(db: Session) -> dict[str, Any]:
    from sqlalchemy import func

    n = db.query(func.count(SolicitationPackage.id)).scalar() or 0
    bytes_sum = db.query(func.coalesce(func.sum(SolicitationPackage.storage_bytes), 0)).scalar() or 0
    docs = db.query(func.count(PackageDocument.id)).scalar() or 0
    downloaded = (
        db.query(func.count(PackageDocument.id)).filter(PackageDocument.download_status == "downloaded").scalar() or 0
    )
    root = get_settings().data_root()
    blob = root / "package_blobs"
    disk = 0
    if blob.exists():
        for p in blob.rglob("*"):
            if p.is_file():
                disk += p.stat().st_size
    return {
        "packages": int(n),
        "documents_rows": int(docs),
        "documents_downloaded": int(downloaded),
        "storage_bytes_tracked": int(bytes_sum),
        "storage_bytes_on_disk": int(disk),
        "data_root": str(root),
    }
