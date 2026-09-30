"""R1.1 file parsers — reuse pdf_text / table_extractors / document_ingestion.

Never executes macros. OCR is detect-only fallback flag (no tesseract required).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc

PARSER_VERSION = "r11-20260929-v1"

# Parse cache keyed by (sha256, parser_version) — avoid reparse of unchanged files
_PARSE_CACHE: dict[str, dict[str, Any]] = {}
_PARSE_CACHE_MAX = 256

FETCHED = "FETCHED"
FETCH_FAILED = "FETCH_FAILED"
AUTH_REQUIRED = "AUTH_REQUIRED"
ANTI_BOT_BLOCKED = "ANTI_BOT_BLOCKED"
NOT_FOUND = "NOT_FOUND"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
EMPTY = "EMPTY"
PARSE_FAILED = "PARSE_FAILED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
OCR_REQUIRED = "OCR_REQUIRED"
PASSWORD_PROTECTED = "PASSWORD_PROTECTED_DOCUMENT"
CONTENT_TYPE_MISMATCH = "CONTENT_TYPE_MISMATCH"
MACRO_ENABLED = "MACRO_ENABLED_BUYER_FILE"


def _utc() -> str:
    return now_utc().isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_content_mismatch(data: bytes, filename: str | None) -> str | None:
    """Reject login/error HTML masquerading as PDF, etc."""
    name = (filename or "").lower()
    head = data[:2048]
    looks_html = bool(re.search(br"<!DOCTYPE\s+html|<html[\s>]|login|sign\s*in|access denied", head, re.I))
    looks_pdf = head.startswith(b"%PDF")
    if name.endswith(".pdf") and not looks_pdf:
        if looks_html:
            return CONTENT_TYPE_MISMATCH
        return CONTENT_TYPE_MISMATCH
    if name.endswith((".html", ".htm")) and looks_pdf:
        return CONTENT_TYPE_MISMATCH
    return None


def detect_scan_pdf(text: str, data: bytes) -> bool:
    """Likely scanned PDF when little/no extractable text on multi-page PDF."""
    if not data.startswith(b"%PDF"):
        return False
    usable = len(re.sub(r"\s+", "", text or ""))
    # crude page estimate
    pages = data.count(b"/Type /Page") or data.count(b"/Type/Page") or 1
    if usable < 40 and pages >= 1:
        return True
    if usable < 20:
        return True
    return False


def _cache_key(data: bytes) -> str:
    return f"{sha256_bytes(data)}:{PARSER_VERSION}"


def get_cached_parse(data: bytes) -> dict[str, Any] | None:
    hit = _PARSE_CACHE.get(_cache_key(data))
    if hit:
        return {**hit, "cache_hit": True}
    return None


def put_cached_parse(data: bytes, result: dict[str, Any]) -> None:
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        # drop arbitrary oldest-ish key
        try:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
        except StopIteration:
            pass
    stored = {k: v for k, v in result.items() if k != "cache_hit"}
    _PARSE_CACHE[_cache_key(data)] = stored


def clear_parse_cache() -> None:
    _PARSE_CACHE.clear()


def parse_file_bytes(
    data: bytes,
    *,
    filename: str,
    allow_ocr_flag: bool = True,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Parse supported solicitation file types into text + structured tables."""
    if use_cache:
        cached = get_cached_parse(data)
        if cached is not None:
            return cached

    mismatch = detect_content_mismatch(data, filename)
    if mismatch:
        result = {
            "ok": False,
            "parse_status": mismatch,
            "text": "",
            "tables": None,
            "method": "rejected",
            "confidence": "UNUSABLE",
            "ocr_required": False,
            "parser_version": PARSER_VERSION,
            "error": mismatch,
            "workbook": None,
            "flags": [mismatch],
        }
        if use_cache:
            put_cached_parse(data, result)
        return result

    ext = Path(filename).suffix.lower()
    flags: list[str] = []
    if ext == ".xlsm" or (filename or "").lower().endswith(".xlsm"):
        flags.append(MACRO_ENABLED)

    # Password-protected Office (encrypted compound) — detect OLE encryption marker loosely
    if ext in {".docx", ".xlsx", ".xls", ".doc"} and b"EncryptedPackage" in data[:8000]:
        result = {
            "ok": False,
            "parse_status": PASSWORD_PROTECTED,
            "text": "",
            "tables": None,
            "method": "blocked",
            "confidence": "UNUSABLE",
            "ocr_required": False,
            "parser_version": PARSER_VERSION,
            "error": PASSWORD_PROTECTED,
            "workbook": None,
            "flags": flags + [PASSWORD_PROTECTED],
        }
        if use_cache:
            put_cached_parse(data, result)
        return result

    try:
        from document_ingestion import _extract_text_and_tables

        extracted = _extract_text_and_tables(data, filename=filename)
    except Exception as exc:
        result = {
            "ok": False,
            "parse_status": PARSE_FAILED,
            "text": "",
            "tables": None,
            "method": "exception",
            "confidence": "UNUSABLE",
            "ocr_required": False,
            "parser_version": PARSER_VERSION,
            "error": str(exc)[:300],
            "workbook": None,
            "flags": flags,
        }
        if use_cache:
            put_cached_parse(data, result)
        return result

    text = (extracted.get("text") or "").strip()
    tables = extracted.get("tables")
    error = extracted.get("error")
    method = "native"
    ocr_required = False
    confidence = "HIGH"

    if ext == ".pdf" or data.startswith(b"%PDF"):
        method = "pdf_native_text"
        if detect_scan_pdf(text, data):
            ocr_required = bool(allow_ocr_flag)
            method = "pdf_scan_detected"
            confidence = "LOW"
            # OCR fallback when scan detected / no usable text
            if not text:
                try:
                    from response_engine.ocr import ocr_pdf_bytes, ocr_engine_status

                    status = ocr_engine_status()
                    if status.get("available"):
                        ocr = ocr_pdf_bytes(data, filename=filename, force_all_pages=True)
                        if ocr.get("ok") and ocr.get("text"):
                            result = {
                                "ok": True,
                                "parse_status": "OCR_FETCHED",
                                "text": ocr["text"],
                                "tables": tables,
                                "method": "pdf_ocr",
                                "confidence": ocr.get("confidence") or "LOW",
                                "ocr_required": True,
                                "ocr": ocr,
                                "parser_version": PARSER_VERSION,
                                "error": None,
                                "workbook": None,
                                "flags": flags + [OCR_REQUIRED],
                                "page_count": ocr.get("stats", {}).get("pages_rendered"),
                            }
                            if use_cache:
                                put_cached_parse(data, result)
                            return result
                        result = {
                            "ok": False,
                            "parse_status": OCR_REQUIRED,
                            "text": "",
                            "tables": tables,
                            "method": "pdf_scan_detected",
                            "confidence": "LOW",
                            "ocr_required": True,
                            "ocr": ocr,
                            "parser_version": PARSER_VERSION,
                            "error": ocr.get("error") or "OCR produced no text",
                            "workbook": None,
                            "flags": flags + [OCR_REQUIRED],
                            "page_count": data.count(b"/Type /Page") or None,
                        }
                        if use_cache:
                            put_cached_parse(data, result)
                        return result
                except Exception as exc:
                    result = {
                        "ok": False,
                        "parse_status": OCR_REQUIRED,
                        "text": text,
                        "tables": tables,
                        "method": "pdf_scan_detected",
                        "confidence": "LOW",
                        "ocr_required": True,
                        "parser_version": PARSER_VERSION,
                        "error": f"OCR engine not available — mark for review ({exc})",
                        "workbook": None,
                        "flags": flags + [OCR_REQUIRED],
                        "page_count": data.count(b"/Type /Page") or None,
                    }
                    if use_cache:
                        put_cached_parse(data, result)
                    return result
                result = {
                    "ok": False if ocr_required else True,
                    "parse_status": OCR_REQUIRED,
                    "text": text,
                    "tables": tables,
                    "method": method,
                    "confidence": "LOW",
                    "ocr_required": True,
                    "parser_version": PARSER_VERSION,
                    "error": "OCR engine not available — mark for review",
                    "workbook": None,
                    "flags": flags + [OCR_REQUIRED],
                    "page_count": data.count(b"/Type /Page") or None,
                }
                if use_cache:
                    put_cached_parse(data, result)
                return result
            # Sparse native text — OCR sparse pages and merge
            flags.append(OCR_REQUIRED)
            confidence = "LOW"
            try:
                from response_engine.ocr import ocr_pdf_bytes, ocr_engine_status

                if ocr_engine_status().get("available"):
                    ocr = ocr_pdf_bytes(data, filename=filename, force_all_pages=False)
                    if ocr.get("text"):
                        text = (text + "\n\n" + ocr["text"]).strip()
                        method = "pdf_native_plus_ocr"
                        confidence = ocr.get("confidence") or "MEDIUM"
                        # stash ocr on result later via extracted side channel
                        extracted["ocr"] = ocr
            except Exception:
                pass

    elif ext in {".xlsx", ".xls", ".xlsm", ".csv"}:
        method = "spreadsheet"
        confidence = "HIGH" if tables and not error else "MEDIUM"
        workbook = _workbook_structure(data, filename) if ext in {".xlsx", ".xlsm"} else None
        result = {
            "ok": not error or bool(text) or bool(tables),
            "parse_status": FETCHED if (text or tables) else (PARSE_FAILED if error else EMPTY),
            "text": text or _tables_as_text(tables),
            "tables": tables,
            "method": method,
            "confidence": confidence,
            "ocr_required": False,
            "parser_version": PARSER_VERSION,
            "error": error,
            "workbook": workbook,
            "flags": flags,
            "is_buyer_template": True,
        }
        if use_cache:
            put_cached_parse(data, result)
        return result

    elif ext in {".docx", ".doc"}:
        method = "docx"
        confidence = "HIGH" if text else "LOW"

    elif ext in {".html", ".htm"} or b"<html" in data[:500].lower():
        method = "html"
        text = text or data.decode("utf-8", errors="replace")
        text = _clean_html_text(text)
        confidence = "MEDIUM"

    elif ext == ".zip" or (data[:2] == b"PK" and ext == ".zip"):
        method = "zip"
        confidence = "MEDIUM" if text else "LOW"

    elif ext == ".txt":
        method = "txt"
        confidence = "HIGH"

    else:
        if not text and not tables:
            result = {
                "ok": False,
                "parse_status": UNSUPPORTED_FORMAT,
                "text": "",
                "tables": None,
                "method": "unsupported",
                "confidence": "UNUSABLE",
                "ocr_required": False,
                "parser_version": PARSER_VERSION,
                "error": UNSUPPORTED_FORMAT,
                "workbook": None,
                "flags": flags,
            }
            if use_cache:
                put_cached_parse(data, result)
            return result

    if not text and not tables:
        status = EMPTY if not error else PARSE_FAILED
        result = {
            "ok": False,
            "parse_status": status,
            "text": "",
            "tables": tables,
            "method": method,
            "confidence": "UNUSABLE",
            "ocr_required": ocr_required,
            "parser_version": PARSER_VERSION,
            "error": error or status,
            "workbook": None,
            "flags": flags,
        }
        if use_cache:
            put_cached_parse(data, result)
        return result

    result = {
        "ok": True,
        "parse_status": OCR_REQUIRED if ocr_required else FETCHED,
        "text": text,
        "tables": tables,
        "method": method,
        "confidence": confidence,
        "ocr_required": ocr_required,
        "parser_version": PARSER_VERSION,
        "error": error,
        "workbook": None,
        "flags": flags,
        "zip_members": extracted.get("members") or [],
        "zip_payload": extracted.get("zip"),
    }
    if extracted.get("ocr"):
        result["ocr"] = extracted["ocr"]
    if use_cache:
        put_cached_parse(data, result)
    return result


def _workbook_structure(data: bytes, filename: str) -> dict[str, Any] | None:
    try:
        import openpyxl
    except ImportError:
        return None
    try:
        wb = openpyxl.load_workbook(io_bytes(data), data_only=False, read_only=True)
        sheets = []
        for name in wb.sheetnames:
            ws = wb[name]
            # sample a few cells with addresses
            cells = []
            for i, row in enumerate(ws.iter_rows(max_row=5, max_col=8)):
                for cell in row:
                    if cell.value is not None:
                        cells.append({"addr": cell.coordinate, "value": str(cell.value)[:200]})
                if i >= 4:
                    break
            sheets.append({"name": name, "sample_cells": cells, "hidden": getattr(ws, "sheet_state", None) == "hidden"})
        wb.close()
        return {
            "filename": filename,
            "sheet_names": [s["name"] for s in sheets],
            "sheets": sheets,
            "formulas_preserved": True,
            "modified": False,
        }
    except Exception as exc:
        return {"filename": filename, "error": str(exc)[:200], "modified": False}


def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def _tables_as_text(tables: dict[str, Any] | None) -> str:
    if not tables:
        return ""
    lines = []
    for li in tables.get("line_items") or []:
        lines.append(str(li.get("description") or li))
    return "\n".join(lines)


def _clean_html_text(html: str) -> str:
    # strip scripts/styles/nav-ish
    html = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def classify_buyer_template(filename: str | None, text: str | None, workbook: dict | None) -> str | None:
    blob = f"{filename or ''} {(text or '')[:800]}".lower()
    sheets = " ".join((workbook or {}).get("sheet_names") or []).lower()
    blob = blob + " " + sheets
    if re.search(r"pric|cost\s*sheet|bid\s*schedule|clin", blob):
        return "PRICING_TEMPLATE"
    if re.search(r"technical\s+response|proposal\s+template", blob):
        return "TECHNICAL_RESPONSE_TEMPLATE"
    if re.search(r"bidder\s+info|offeror\s+info|vendor\s+info", blob):
        return "BIDDER_INFORMATION_FORM"
    if re.search(r"certif|representation", blob):
        return "CERTIFICATION_FORM"
    if re.search(r"signat", blob):
        return "SIGNATURE_FORM"
    if re.search(r"deliver", blob):
        return "DELIVERY_TEMPLATE"
    if (filename or "").lower().endswith((".xlsx", ".xls", ".csv", ".xlsm")):
        return "OTHER_BUYER_TEMPLATE"
    return None
