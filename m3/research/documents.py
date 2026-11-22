"""Reusable document text extraction for classification evidence (Stage 4)."""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any
from urllib.parse import urlparse

from m3.research.http_util import fetch_text


def _clean(text: str, limit: int = 12000) -> str:
    text = (text or "").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def extract_html_text(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text)


def extract_pdf_text(data: bytes, *, allow_ocr: bool = True) -> dict[str, Any]:
    try:
        from pdfminer.high_level import extract_text as pdf_extract

        text = pdf_extract(io.BytesIO(data)) or ""
        cleaned = _clean(text)
        if len(cleaned) >= 40:
            return {"ok": True, "text": cleaned, "format": "pdf", "reason": None, "method": "pdfminer"}
        # OCR fallback for scanned / image PDFs (Build 002H)
        if allow_ocr:
            try:
                from m3.research.extract_fields import ocr_pdf_bytes

                ocr = ocr_pdf_bytes(data, max_pages=5)
                if ocr.get("ok") and ocr.get("text"):
                    return {
                        "ok": True,
                        "text": _clean(str(ocr["text"])),
                        "format": "pdf",
                        "reason": None,
                        "method": "ocr_fallback",
                        "ocr_meta": {k: ocr.get(k) for k in ("pages_ocrd", "method")},
                    }
                return {
                    "ok": False,
                    "reason": ocr.get("reason") or "pdf_little_or_no_extractable_text_may_be_scanned",
                    "text": cleaned,
                    "format": "pdf",
                }
            except Exception as ocr_exc:
                return {
                    "ok": False,
                    "reason": f"pdf_thin_ocr_error:{type(ocr_exc).__name__}",
                    "text": cleaned,
                    "format": "pdf",
                }
        return {
            "ok": False,
            "reason": "pdf_little_or_no_extractable_text_may_be_scanned",
            "text": cleaned,
            "format": "pdf",
        }
    except Exception as exc:
        return {"ok": False, "reason": f"pdf_extract_error:{type(exc).__name__}", "text": "", "format": "pdf"}


def extract_docx_text(data: bytes) -> dict[str, Any]:
    try:
        import docx  # python-docx

        doc = docx.Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = _clean("\n".join(parts))
        return {"ok": bool(text), "text": text, "format": "docx", "reason": None if text else "empty_docx"}
    except Exception as exc:
        return {"ok": False, "reason": f"docx_extract_error:{type(exc).__name__}", "text": "", "format": "docx"}


def extract_xlsx_text(data: bytes) -> dict[str, Any]:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []
        for ws in wb.worksheets[:5]:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 200:
                    break
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
        text = _clean("\n".join(parts))
        return {"ok": bool(text), "text": text, "format": "xlsx", "reason": None if text else "empty_xlsx"}
    except Exception as exc:
        return {"ok": False, "reason": f"xlsx_extract_error:{type(exc).__name__}", "text": "", "format": "xlsx"}


def extract_csv_or_txt(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    cleaned = _clean(text)
    return {"ok": bool(cleaned), "text": cleaned, "format": "text", "reason": None if cleaned else "empty_text"}


def guess_format(url: str, content_type: str = "", magic: bytes = b"") -> str:
    path = urlparse(url or "").path.lower()
    ct = (content_type or "").lower()
    if path.endswith(".pdf") or "pdf" in ct or magic.startswith(b"%PDF"):
        return "pdf"
    if path.endswith(".docx") or "wordprocessingml" in ct:
        return "docx"
    if path.endswith((".xlsx", ".xls")) or "spreadsheetml" in ct or "excel" in ct:
        return "xlsx"
    if path.endswith((".csv", ".txt")) or "text/csv" in ct or "text/plain" in ct:
        return "text"
    if "html" in ct or path.endswith((".html", ".htm")) or (magic[:1] == b"<" or b"<html" in magic[:200].lower()):
        return "html"
    return "unknown"


def fetch_and_extract(url: str, *, timeout: float = 18.0) -> dict[str, Any]:
    """Fetch a URL and extract classification-relevant text. Never claims success on failure."""
    if not url or not str(url).startswith("http"):
        return {"ok": False, "reason": "missing_or_invalid_url", "text": "", "url": url}
    # Skip raw Socrata JSON endpoints as "documents"
    if ".json" in url.split("?")[0].lower() and "resource/" in url:
        return {"ok": False, "reason": "socrata_json_not_document", "text": "", "url": url}
    cache_key = "doc_extract:" + hashlib.sha256(url.encode()).hexdigest()[:24]
    try:
        # Binary-capable fetch via http_util text path may mangle PDFs — use urllib for bytes
        import urllib.request

        from m3.config import get_settings

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "M3SupplyResearch/2.0 (+classification-evidence)"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout or get_settings().research_timeout_seconds) as resp:
            status = getattr(resp, "status", 200) or 200
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(8_000_000)
            final = resp.geturl()
        if status >= 400:
            return {"ok": False, "reason": f"http_status_{status}", "text": "", "url": final, "cache_key": cache_key}
        fmt = guess_format(final, ctype, data[:32])
        if fmt == "pdf":
            out = extract_pdf_text(data)
        elif fmt == "docx":
            out = extract_docx_text(data)
        elif fmt == "xlsx":
            out = extract_xlsx_text(data)
        elif fmt == "text":
            out = extract_csv_or_txt(data)
        elif fmt == "html":
            text = extract_html_text(data.decode("utf-8", errors="replace"))
            out = {
                "ok": len(text) >= 40,
                "text": text,
                "format": "html",
                "reason": None if len(text) >= 40 else "html_too_thin",
            }
        else:
            # Try HTML then text
            try:
                text = extract_html_text(data.decode("utf-8", errors="replace"))
                if len(text) >= 40:
                    out = {"ok": True, "text": text, "format": "html", "reason": None}
                else:
                    out = extract_csv_or_txt(data)
            except Exception:
                out = {"ok": False, "reason": "unsupported_or_binary", "text": "", "format": fmt}
        out["url"] = final
        out["cache_key"] = cache_key
        out["content_type"] = ctype
        return out
    except Exception as exc:
        # Fallback to text fetch for HTML pages
        try:
            status, final, html = fetch_text(url, timeout=timeout)
            if status < 400 and html:
                text = extract_html_text(html)
                return {
                    "ok": len(text) >= 40,
                    "text": text,
                    "format": "html",
                    "url": final,
                    "cache_key": cache_key,
                    "reason": None if len(text) >= 40 else "html_too_thin",
                }
        except Exception:
            pass
        return {
            "ok": False,
            "reason": f"fetch_error:{type(exc).__name__}",
            "text": "",
            "url": url,
            "cache_key": cache_key,
        }


def find_attachment_urls(html: str, base_url: str) -> list[str]:
    """Pull likely solicitation attachment links from a detail HTML page."""
    from urllib.parse import urljoin

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html or "", flags=re.I)
    out: list[str] = []
    seen: set[str] = set()
    for h in hrefs:
        abs_u = urljoin(base_url, h)
        low = abs_u.lower()
        if any(low.split("?")[0].endswith(ext) for ext in (".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".doc")):
            if abs_u not in seen:
                seen.add(abs_u)
                out.append(abs_u)
        elif re.search(r"(?i)(attachment|document|download|solicitation|specification|addendum)", low):
            if abs_u not in seen and abs_u.startswith("http"):
                seen.add(abs_u)
                out.append(abs_u)
    return out[:12]
