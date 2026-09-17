"""Structured extraction from tables/spreadsheets/archives for bid schedules.

Never converts empty bidder-price fields to $0 — use PRICE_TO_BE_PROVIDED.
"""

from __future__ import annotations
from application_clock import now_utc

import csv
import hashlib
import html as html_lib
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from solicitation_package_constants import PRICE_TO_BE_PROVIDED, TABLE_EXTRACTION_NEEDS_REVIEW


def _utc() -> str:
    return now_utc().isoformat()


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_price_cell(raw: Any) -> dict[str, Any]:
    """Empty / blank price cells are PRICE_TO_BE_PROVIDED, never 0."""
    if raw is None:
        return {"value": None, "status": PRICE_TO_BE_PROVIDED, "raw": None}
    if isinstance(raw, (int, float)):
        return {"value": float(raw), "status": "NUMERIC", "raw": raw}
    text = str(raw).strip()
    if not text or text in {"-", "—", "N/A", "n/a", "$", "."}:
        return {"value": None, "status": PRICE_TO_BE_PROVIDED, "raw": text or None}
    cleaned = re.sub(r"[,$]", "", text)
    try:
        return {"value": float(cleaned), "status": "NUMERIC", "raw": text}
    except ValueError:
        if re.search(r"\b(enter|bid|price|tbd|to be)\b", text, re.I):
            return {"value": None, "status": PRICE_TO_BE_PROVIDED, "raw": text}
        return {"value": None, "status": "UNKNOWN", "raw": text}


def extract_csv_table(data: bytes | str, *, source_name: str | None = None) -> dict[str, Any]:
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data
    reader = csv.reader(io.StringIO(text))
    rows = [list(r) for r in reader]
    return _rows_to_schedule(rows, source_name=source_name, method="csv")


def extract_xlsx_table(data: bytes, *, source_name: str | None = None) -> dict[str, Any]:
    try:
        import openpyxl
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"openpyxl_unavailable:{exc}",
            "rows": [],
            "line_items": [],
            "source_name": source_name,
            "extraction_method": "xlsx",
        }
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.active
    rows = [[cell if cell is not None else "" for cell in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    return _rows_to_schedule(rows, source_name=source_name, method="xlsx")


def extract_xls_table(data: bytes, *, source_name: str | None = None) -> dict[str, Any]:
    """Legacy .xls — inventory only unless xlrd available; never fabricate."""
    try:
        import xlrd  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "error": "xls_unsupported_without_xlrd",
            "access_hint": "UNSUPPORTED",
            "rows": [],
            "line_items": [],
            "source_name": source_name,
            "extraction_method": "xls",
        }
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    rows = [sheet.row_values(i) for i in range(sheet.nrows)]
    return _rows_to_schedule(rows, source_name=source_name, method="xls")


def extract_html_tables(html: str, *, source_name: str | None = None) -> dict[str, Any]:
    tables: list[list[list[str]]] = []
    for tm in re.finditer(r"<table\b[^>]*>(.*?)</table>", html or "", re.I | re.S):
        body = tm.group(1)
        rows: list[list[str]] = []
        for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", body, re.I | re.S):
            cells = re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.I | re.S)
            cleaned = [
                re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                for c in cells
            ]
            if any(cleaned):
                rows.append(cleaned)
        if rows:
            tables.append(rows)
    if not tables:
        return {
            "ok": False,
            "error": "no_html_tables",
            "rows": [],
            "line_items": [],
            "tables": [],
            "source_name": source_name,
            "extraction_method": "html_table",
        }
    # Prefer the largest table as primary schedule
    primary = max(tables, key=lambda t: len(t))
    result = _rows_to_schedule(primary, source_name=source_name, method="html_table")
    result["tables_found"] = len(tables)
    result["all_tables_row_counts"] = [len(t) for t in tables]
    return result


def extract_docx_tables(data: bytes, *, source_name: str | None = None) -> dict[str, Any]:
    try:
        from docx import Document
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"python_docx_unavailable:{exc}",
            "rows": [],
            "line_items": [],
            "source_name": source_name,
            "extraction_method": "docx_table",
        }
    doc = Document(io.BytesIO(data))
    if not doc.tables:
        # Fallback: paragraph text only
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return {
            "ok": bool(text),
            "rows": [],
            "line_items": [],
            "raw_text": text[:50_000],
            "source_name": source_name,
            "extraction_method": "docx_paragraphs",
            "error": None if text else "no_docx_tables",
        }
    primary = None
    for table in doc.tables:
        rows = [[(cell.text or "").strip() for cell in row.cells] for row in table.rows]
        if primary is None or len(rows) > len(primary):
            primary = rows
    return _rows_to_schedule(primary or [], source_name=source_name, method="docx_table")


def extract_pdf_tables_heuristic(text: str, *, source_name: str | None = None) -> dict[str, Any]:
    """
    Heuristic table/line extraction from PDF text.
    Marks TABLE_EXTRACTION_NEEDS_REVIEW when structure is ambiguous.
    """
    from transactional_bom import extract_sciquest_product_line_items

    items = extract_sciquest_product_line_items(text or "", source_document=source_name)
    needs_review = not items or any(
        (it.get("quantity") is None and "qty" not in (it.get("description") or "").lower())
        for it in items
    )
    return {
        "ok": bool(items),
        "line_items": items,
        "rows": [],
        "source_name": source_name,
        "extraction_method": "pdf_text_heuristic",
        "review_flag": TABLE_EXTRACTION_NEEDS_REVIEW if needs_review and items else None,
        "raw_context": (text or "")[:8000],
        "error": None if items else "no_line_items_detected",
    }


def _rows_to_schedule(
    rows: list[list[Any]],
    *,
    source_name: str | None,
    method: str,
) -> dict[str, Any]:
    if not rows:
        return {
            "ok": False,
            "error": "empty_table",
            "rows": [],
            "line_items": [],
            "source_name": source_name,
            "extraction_method": method,
        }
    header = [str(c).strip().lower() for c in rows[0]]
    col_map = _map_columns(header)
    line_items: list[dict[str, Any]] = []
    # If header doesn't look like a header, treat all rows as data
    data_rows = rows[1:] if col_map else rows
    if not col_map:
        # synthesize columns by position
        for i, row in enumerate(rows):
            if not any(str(c).strip() for c in row):
                continue
            desc = " | ".join(str(c).strip() for c in row if str(c).strip())
            price_cell = _normalize_price_cell(row[-1] if row else None)
            line_items.append(
                {
                    "line_number": str(i + 1),
                    "description": desc[:500],
                    "quantity": None,
                    "unit_of_measure": None,
                    "unit_price": price_cell,
                    "extraction_method": method,
                    "source_document": source_name,
                }
            )
    else:
        for i, row in enumerate(data_rows):
            if not any(str(c).strip() for c in row if c is not None):
                continue

            def cell(key: str) -> Any:
                idx = col_map.get(key)
                if idx is None or idx >= len(row):
                    return None
                return row[idx]

            qty_raw = cell("quantity")
            qty = None
            if qty_raw is not None and str(qty_raw).strip():
                try:
                    qty = float(re.sub(r"[^\d.]", "", str(qty_raw)))
                except ValueError:
                    qty = None
            price_cell = _normalize_price_cell(cell("unit_price"))
            line_items.append(
                {
                    "line_number": str(cell("line_number") or i + 1),
                    "CLIN_or_item_number": cell("item_number"),
                    "description": (str(cell("description") or "").strip() or None),
                    "manufacturer": cell("manufacturer"),
                    "model": cell("model"),
                    "part_number": cell("part_number"),
                    "quantity": qty,
                    "unit_of_measure": cell("uom"),
                    "unit_price": price_cell,
                    "extraction_method": method,
                    "source_document": source_name,
                }
            )
    return {
        "ok": True,
        "rows": [[("" if c is None else c) for c in r] for r in rows],
        "line_items": line_items,
        "header": header if col_map else None,
        "column_map": col_map,
        "source_name": source_name,
        "extraction_method": method,
        "error": None,
    }


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    patterns = {
        "line_number": r"^(line|#|item\s*#|no\.?)$",
        "item_number": r"(clin|item\s*(number|no|#)|sku|part)",
        "description": r"(desc|description|item\s*name|product)",
        "quantity": r"(qty|quantity|qty\.|amount)",
        "uom": r"(uom|unit|units)",
        "unit_price": r"(unit\s*price|price|bid\s*price|unit\s*cost|\$)",
        "manufacturer": r"(mfr|manufacturer|brand)",
        "model": r"(model)",
        "part_number": r"(part\s*(number|no|#)|p/?n)",
    }
    for idx, h in enumerate(header):
        for key, pat in patterns.items():
            if key in mapping:
                continue
            if re.search(pat, h, re.I):
                mapping[key] = idx
    # Require at least description or item to treat as headered
    if "description" not in mapping and "item_number" not in mapping and "quantity" not in mapping:
        return {}
    return mapping


# ---------------------------------------------------------------------------
# ZIP safe extraction
# ---------------------------------------------------------------------------


def safe_inspect_zip(
    data: bytes,
    *,
    max_members: int = 200,
    max_total_uncompressed: int = 50 * 1024 * 1024,
    max_member_bytes: int = 25 * 1024 * 1024,
) -> dict[str, Any]:
    """
    Safely inspect a ZIP. Rejects path traversal. Does not execute contents.
    Returns inventory + extracted supported members as bytes (in memory).
    """
    inventory: list[dict[str, Any]] = []
    extracted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    total_uncompressed = 0

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return {
            "ok": False,
            "error": f"bad_zip:{exc}",
            "inventory": [],
            "extracted": [],
            "rejected": [],
        }

    with zf:
        names = zf.namelist()
        if len(names) > max_members:
            return {
                "ok": False,
                "error": "too_many_members",
                "inventory": [],
                "extracted": [],
                "rejected": [{"reason": "too_many_members", "count": len(names)}],
            }
        for info in zf.infolist():
            name = info.filename
            entry = {
                "name": name,
                "is_dir": info.is_dir(),
                "compressed_size": info.compress_size,
                "file_size": info.file_size,
            }
            if info.is_dir():
                inventory.append(entry)
                continue
            # Path traversal / absolute path rejection
            posix = PurePosixPath(name.replace("\\", "/"))
            if posix.is_absolute() or ".." in posix.parts or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                rejected.append({**entry, "reason": "path_traversal_or_absolute"})
                inventory.append({**entry, "safe": False, "reason": "path_traversal_or_absolute"})
                continue
            if info.file_size > max_member_bytes:
                rejected.append({**entry, "reason": "member_too_large"})
                inventory.append({**entry, "safe": False, "reason": "member_too_large"})
                continue
            if total_uncompressed + info.file_size > max_total_uncompressed:
                rejected.append({**entry, "reason": "archive_uncompressed_budget"})
                inventory.append({**entry, "safe": False, "reason": "archive_uncompressed_budget"})
                continue
            try:
                payload = zf.read(info)
            except Exception as exc:
                rejected.append({**entry, "reason": f"read_error:{exc}"})
                inventory.append({**entry, "safe": False, "reason": "read_error"})
                continue
            total_uncompressed += len(payload)
            ext = PurePosixPath(name).suffix.lower()
            inventory.append({**entry, "safe": True, "extension": ext, "sha256": content_sha256(payload)})
            extracted.append(
                {
                    "name": name,
                    "safe_name": posix.name,
                    "extension": ext,
                    "bytes": payload,
                    "sha256": content_sha256(payload),
                    "size": len(payload),
                }
            )

    return {
        "ok": True,
        "error": None,
        "inventory": inventory,
        "extracted": extracted,
        "rejected": rejected,
        "total_uncompressed": total_uncompressed,
        "inspected_at": _utc(),
    }


def extract_from_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Dispatch extraction by filename / magic."""
    name = (filename or "document").lower()
    if name.endswith(".csv") or (content_type or "").endswith("csv"):
        return extract_csv_table(data, source_name=filename)
    if name.endswith(".xlsx"):
        return extract_xlsx_table(data, source_name=filename)
    if name.endswith(".xls"):
        return extract_xls_table(data, source_name=filename)
    if name.endswith(".docx"):
        return extract_docx_tables(data, source_name=filename)
    if name.endswith(".html") or name.endswith(".htm") or data[:200].lower().find(b"<table") >= 0:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return extract_html_tables(text, source_name=filename)
    if name.endswith(".pdf") or data.startswith(b"%PDF"):
        from pdf_text import extract_pdf_text

        text = extract_pdf_text(data) or ""
        return extract_pdf_tables_heuristic(text, source_name=filename)
    if name.endswith(".zip") or data[:2] == b"PK":
        return safe_inspect_zip(data)
    if name.endswith(".txt"):
        text = data.decode("utf-8", errors="replace")
        return extract_pdf_tables_heuristic(text, source_name=filename)
    return {
        "ok": False,
        "error": "unsupported_type",
        "line_items": [],
        "rows": [],
        "source_name": filename,
    }
