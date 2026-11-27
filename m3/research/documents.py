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


_QTY_HEADER_RE = re.compile(
    r"(?i)^("
    r"quantity|qty\.?|est\.?\s*qty|estimated\s+(?:quantity|usage|qty|annual\s+usage)|"
    r"annual\s+(?:usage|qty|quantity)|estimated\s+usage|usage|order\s+qty|"
    r"qty\s+required|required\s+qty|no\.?\s*of\s*units|units?"
    r")$"
)
_ITEM_HEADER_RE = re.compile(
    r"(?i)^(item\s+description|description|item|product|commodity|grade/?type|grade|type)$"
)
_UOM_HEADER_RE = re.compile(r"(?i)^(uom|u/?m|unit(?:\s+of\s+measure)?|units?)$")


def _xlsx_map_headers(lower_cells: list[str]) -> dict[str, int]:
    header_idx: dict[str, int] = {}
    for j, h in enumerate(lower_cells):
        h = (h or "").strip()
        if not h:
            continue
        if _QTY_HEADER_RE.match(h) and "quantity" not in header_idx:
            header_idx["quantity"] = j
        elif _ITEM_HEADER_RE.match(h) and "item" not in header_idx:
            header_idx["item"] = j
        elif _UOM_HEADER_RE.match(h) and "uom" not in header_idx:
            header_idx["uom"] = j
        elif h in {"#", "item #", "line", "line #", "line no", "line no.", "line number"}:
            header_idx.setdefault("line", j)
    return header_idx


def extract_xls_text(data: bytes) -> dict[str, Any]:
    """Extract legacy .xls cell text via xlrd. Never utf-8-decode OLE bytes."""
    try:
        import xlrd
    except ImportError:
        return {
            "ok": False,
            "reason": "xls_needs_xlrd",
            "text": "",
            "format": "xls",
        }
    try:
        book = xlrd.open_workbook(file_contents=data)
        parts: list[str] = []
        qty_lines: list[str] = []
        schedule_rows: list[dict[str, Any]] = []
        for sheet in book.sheets()[:8]:
            parts.append(f"[worksheet:{sheet.name}]")
            header_idx: dict[str, int] = {}
            for r in range(min(int(sheet.nrows), 400)):
                cells: list[Any] = []
                for c in range(int(sheet.ncols)):
                    val = sheet.cell_value(r, c)
                    if val is None or val == "":
                        cells.append(None)
                    else:
                        cells.append(val)
                nonempty = [str(c) for c in cells if c is not None and str(c).strip()]
                if not nonempty:
                    continue
                lower_cells = [str(c or "").strip().lower() for c in cells]
                if not header_idx:
                    mapped = _xlsx_map_headers(lower_cells)
                    if "quantity" in mapped or any(
                        h in lower_cells
                        for h in (
                            "item description",
                            "item",
                            "bid/no bid decision",
                            "grade/type",
                            "facility name & address",
                        )
                    ):
                        header_idx = mapped
                parts.append(" | ".join(nonempty))
                if "quantity" in header_idx:
                    qcell = cells[header_idx["quantity"]] if header_idx["quantity"] < len(cells) else None
                    if qcell is not None and re.search(r"^\s*[\d,]+(?:\.\d+)?\s*$", str(qcell)):
                        item = ""
                        if "item" in header_idx and header_idx["item"] < len(cells) and cells[header_idx["item"]]:
                            item = str(cells[header_idx["item"]]).strip()[:80]
                        uom = "EA"
                        if "uom" in header_idx and header_idx["uom"] < len(cells) and cells[header_idx["uom"]]:
                            uom = str(cells[header_idx["uom"]]).strip() or "EA"
                        qnum = str(qcell).strip().replace(",", "")
                        qty_lines.append(
                            f"Quantity: {qnum} {uom}" + (f" line_item:{item}" if item else "")
                        )
                        try:
                            qf = float(qnum)
                        except ValueError:
                            qf = None
                        if qf is not None and qf > 0:
                            schedule_rows.append(
                                {"quantity": qf, "uom": uom, "item": item, "sheet": sheet.name}
                            )
        schedule_summary = summarize_schedule_quantities(schedule_rows)
        if qty_lines:
            seen: set[str] = set()
            uniq: list[str] = []
            for q in qty_lines[:120]:
                if q not in seen:
                    seen.add(q)
                    uniq.append(q)
            summary_lines = []
            if schedule_summary.get("title_quantity"):
                summary_lines.append(f"SCHEDULE_TOTAL_QUANTITY: {schedule_summary['title_quantity']}")
            if schedule_summary.get("line_count"):
                summary_lines.append(f"SCHEDULE_LINE_COUNT: {schedule_summary['line_count']}")
            parts = ["[extracted_quantity_lines]", *summary_lines, *uniq, *parts]
        raw = "\n".join(parts)
        text = (raw or "").replace("\x00", "")
        text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
        if len(text) > 40000:
            text = text[:40000]
        return {
            "ok": bool(text),
            "text": text,
            "format": "xls",
            "reason": None if text else "empty_xls",
            "quantity_lines": qty_lines[:80] if qty_lines else [],
            "schedule_rows": schedule_rows[:200],
            "schedule_summary": schedule_summary,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"xls_extract_error:{type(exc).__name__}",
            "text": "",
            "format": "xls",
        }


def extract_xlsx_text(data: bytes) -> dict[str, Any]:
    """Extract spreadsheet text preserving sheet/row structure for quantity schedules.

    Also emits affirmative Quantity: N lines when a Quantity / Estimated Usage
    column (with optional UoM) is present in pricing / bid-response sheets.
    """
    # Legacy BIFF .xls must not go through openpyxl / utf-8 decode.
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return extract_xls_text(data)
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []
        qty_lines: list[str] = []
        schedule_rows: list[dict[str, Any]] = []
        for ws in wb.worksheets[:8]:
            parts.append(f"[worksheet:{ws.title}]")
            header_idx: dict[str, int] = {}
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 400:
                    break
                cells = list(row)
                nonempty = [str(c) for c in cells if c is not None and str(c).strip()]
                if not nonempty:
                    continue
                lower_cells = [str(c or "").strip().lower() for c in cells]
                # Header detection — Quantity OR Estimated Usage / Annual Usage, etc.
                if not header_idx:
                    mapped = _xlsx_map_headers(lower_cells)
                    if "quantity" in mapped or any(
                        h in lower_cells
                        for h in (
                            "item description",
                            "item",
                            "bid/no bid decision",
                            "grade/type",
                            "facility name & address",
                        )
                    ):
                        header_idx = mapped
                line = " | ".join(nonempty)
                parts.append(line)
                # Explicit Quantity / Estimated Usage column
                if "quantity" in header_idx:
                    qcell = cells[header_idx["quantity"]] if header_idx["quantity"] < len(cells) else None
                    if qcell is not None and re.search(r"^\s*[\d,]+(?:\.\d+)?\s*$", str(qcell)):
                        item = ""
                        if "item" in header_idx and header_idx["item"] < len(cells) and cells[header_idx["item"]]:
                            item = str(cells[header_idx["item"]]).strip()[:80]
                        uom = "EA"
                        if "uom" in header_idx and header_idx["uom"] < len(cells) and cells[header_idx["uom"]]:
                            uom = str(cells[header_idx["uom"]]).strip() or "EA"
                        qnum = str(qcell).strip().replace(",", "")
                        qty_lines.append(
                            f"Quantity: {qnum} {uom}" + (f" line_item:{item}" if item else "")
                        )
                        try:
                            qf = float(qnum)
                        except ValueError:
                            qf = None
                        if qf is not None and qf > 0:
                            schedule_rows.append(
                                {"quantity": qf, "uom": uom, "item": item, "sheet": ws.title}
                            )
                # Pricing sheets often encode ordered counts as trailing (N) in item text
                for cell in nonempty:
                    m = re.search(
                        r"(?i)^(.{8,120}?)\((\d{1,5})\)\s*$",
                        cell.strip(),
                    )
                    if m and not re.search(r"(?i)\b(lb|lbs|inch|inches|mm|cm|volt|amp|watt|year|gvwr)\b", m.group(1)):
                        # e.g. "Nike Practice Soccer Balls Size: 5 (30)"
                        qty_lines.append(
                            f"Quantity: {m.group(2)} EA line_item:{m.group(1).strip()[:80]}"
                        )
                        schedule_rows.append(
                            {
                                "quantity": float(m.group(2)),
                                "uom": "EA",
                                "item": m.group(1).strip()[:80],
                                "sheet": ws.title,
                            }
                        )
        schedule_summary = summarize_schedule_quantities(schedule_rows)
        if qty_lines:
            # Deduplicate while preserving order
            seen: set[str] = set()
            uniq: list[str] = []
            for q in qty_lines[:120]:
                if q not in seen:
                    seen.add(q)
                    uniq.append(q)
            summary_lines = []
            if schedule_summary.get("title_quantity"):
                summary_lines.append(f"SCHEDULE_TOTAL_QUANTITY: {schedule_summary['title_quantity']}")
            if schedule_summary.get("line_count"):
                summary_lines.append(f"SCHEDULE_LINE_COUNT: {schedule_summary['line_count']}")
            # Prepend quantity lines so _clean truncation cannot drop them
            parts = ["[extracted_quantity_lines]", *summary_lines, *uniq, *parts]
        # Keep newlines in quantity block — use a higher limit and preserve structure.
        raw = "\n".join(parts)
        text = (raw or "").replace("\x00", "")
        text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
        if len(text) > 40000:
            text = text[:40000]
        return {
            "ok": bool(text),
            "text": text,
            "format": "xlsx",
            "reason": None if text else "empty_xlsx",
            "quantity_lines": qty_lines[:80] if qty_lines else [],
            "schedule_rows": schedule_rows[:200],
            "schedule_summary": schedule_summary,
        }
    except Exception as exc:
        return {"ok": False, "reason": f"xlsx_extract_error:{type(exc).__name__}", "text": "", "format": "xlsx"}


def summarize_schedule_quantities(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse schedule rows into a title-friendly quantity string."""
    if not rows:
        return {}
    by_uom: dict[str, float] = {}
    for r in rows:
        uom = str(r.get("uom") or "EA").strip() or "EA"
        try:
            q = float(r.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        by_uom[uom] = by_uom.get(uom, 0.0) + q
    if not by_uom:
        return {"line_count": len(rows)}
    parts: list[str] = []
    for uom, total in by_uom.items():
        if float(total).is_integer():
            parts.append(f"{int(total):,} {uom}")
        else:
            parts.append(f"{total:g} {uom}")
    title_qty = " + ".join(parts)
    return {
        "line_count": len(rows),
        "totals_by_uom": {k: (int(v) if float(v).is_integer() else v) for k, v in by_uom.items()},
        "title_quantity": title_qty,
        "is_multi_line": len(rows) >= 2,
    }


_USELESS_AI_QTY = re.compile(
    r"(?i)^("
    r"unknown|n/?a|none|null|tbd|not\s+specified\.?|see\s+(?:bid|docs?|attachment|schedule)|"
    r"approximate(?:\s+and\s+not\s+guaranteed)?.*|"
    r"various\s+quantit(?:y|ies).*|"
    r"not\s+explicitly\s+mentioned\.?"
    r")\s*$"
)


def quantity_is_unspecified(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) <= 0
    text = str(value).strip()
    if not text:
        return True
    return bool(_USELESS_AI_QTY.match(text))


def merge_schedule_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for s in summaries:
        if not s:
            continue
        totals = s.get("totals_by_uom") or {}
        if isinstance(totals, dict) and totals:
            for uom, total in totals.items():
                try:
                    q = float(total)
                except (TypeError, ValueError):
                    continue
                if q > 0:
                    rows.append({"quantity": q, "uom": uom})
            continue
        title_q = str(s.get("title_quantity") or "").strip()
        if title_q:
            for part in re.split(r"\s*\+\s*", title_q):
                m = re.match(r"^\s*([\d,]+(?:\.\d+)?)\s+(\S+)\s*$", part)
                if m:
                    rows.append(
                        {
                            "quantity": float(m.group(1).replace(",", "")),
                            "uom": m.group(2),
                        }
                    )
    return summarize_schedule_quantities(rows)


def schedule_quantity_from_paths(paths: list[Any]) -> dict[str, Any]:
    """Deterministic quantity from local bid sheets (xlsx/xls). No OpenAI."""
    from pathlib import Path

    summaries: list[dict[str, Any]] = []
    all_lines: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        low = path.name.lower()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if low.endswith((".xlsx", ".xlsm")) or (data[:2] == b"PK" and low.endswith((".xlsx", ".xlsm"))):
            ex = extract_xlsx_text(data)
        elif low.endswith(".xls") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            ex = extract_xls_text(data)
        else:
            continue
        if ex.get("schedule_summary"):
            summaries.append(ex["schedule_summary"])
        for line in ex.get("quantity_lines") or []:
            if line not in all_lines:
                all_lines.append(line)
    summary = merge_schedule_summaries(summaries)
    if summary:
        summary = {**summary, "quantity_lines": all_lines[:80], "source": "bid_sheet_schedule"}
    return summary


def prefer_schedule_quantity(ai_quantity: Any, schedule: dict[str, Any] | None) -> Any:
    """Hard rule: bid-sheet schedule wins over AI 'Not specified' / empty."""
    title_q = str((schedule or {}).get("title_quantity") or "").strip()
    if not title_q:
        return ai_quantity
    if quantity_is_unspecified(ai_quantity):
        return title_q
    # Prefer schedule total when AI only echoed a vague estimate phrase.
    ai_text = str(ai_quantity).strip()
    if re.search(r"(?i)\b(not\s+specified|see\s+attachment|various)\b", ai_text):
        return title_q
    return ai_quantity


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
