"""Content-first product-schedule recognition for BidNet packages.

Filename/role heuristics are secondary. Every valid local document is inspected
for product-line tables, CLIN/MPN/qty structures, and catalog-discount patterns.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

BUILD = "20261007-m3-same20-full-pipeline-recovery-v1"
PARSER_CACHE_VERSION = "s20-content-v2"
_PARSE_CACHE_FILE = "m3_schedule_parse_cache_v1.json"

PRODUCT_ROLES = {
    "PRODUCT_SCHEDULE",
    "PRICING_SCHEDULE",
    "BID_FORM",
    "ITEM_LIST",
    "SPECIFICATION_WITH_PRODUCT_TABLE",
    "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES",
    "CATALOG_REFERENCE",
}

SIGNAL_TERMS = (
    "item", "line", "clin", "bid item", "description", "manufacturer", "model",
    "part number", "part no", "mpn", "sku", "nsn", "quantity", "qty", "unit",
    "uom", "pack", "each", " ea ", "case", "box", "set", "lot", "extended price",
    "unit price", "brand", "or equal", "product", "equipment", "material", "supply",
    "catalog", "oem",
)

_QTY_UOM = re.compile(
    r"\b(?P<qty>\d[\d,]*(?:\.\d+)?)\s*(?P<uom>EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF|GAL|LB|SET|KIT|PAIR|LOT|UN)\b",
    re.I,
)
_LABELED_PN = re.compile(
    r"\b(?:MPN|P/?N|PART\s*NO\.?|PART\s*NUMBER|MODEL(?:\s*NO\.?)?|CAT(?:ALOG)?\s*NO\.?|SKU|NSN)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9\-./]{2,})\b",
    re.I,
)
_ITEM_ROW = re.compile(
    r"^\s*(?P<item>\d{1,4}[A-Za-z]?|[A-Z]\d{1,3}|CLIN\s*\d+)\s+"
    r"(?P<desc>.{8,220}?)\s+"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<uom>EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF|GAL|LB|SET|KIT|PAIR|LOT)?\b",
    re.I,
)
# Tab/multi-space separated item rows common in PDF text extraction
_ITEM_ROW_LOOSE = re.compile(
    r"^\s*(?P<item>\d{1,4})\s{2,}(?P<desc>.{6,200}?)\s{2,}(?P<qty>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s+(?P<uom>EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF|GAL|LB|SET|KIT|PAIR|LOT))?\b",
    re.I,
)
_CLIN = re.compile(r"\bCLIN\s*[:#]?\s*(\d{1,6})\b", re.I)
_CATALOG_DISCOUNT = re.compile(
    r"\b(percent(?:age)?\s+discount|discount\s+(?:off|from|of)\s+(?:list|catalog|msrp|oem)|"
    r"catalog\s+discount|percentage\s+off|%?\s*off\s+(?:list|catalog|oem)|"
    r"discount\s+from\s+(?:the\s+)?(?:manufacturer|oem)\s+catalog|"
    r"manufacturer(?:'s)?\s+(?:current\s+)?(?:price\s+)?list|"
    r"oem\s+(?:price\s+)?list|list\s+price\s+less|"
    r"percentage\s+off\s+(?:the\s+)?(?:manufacturer|dealer|oem)|"
    r"bid\s+a\s+percentage| bid\s+percentage|%?\s*discount\s+from)\b",
    re.I,
)
_SERVICE_ONLY = re.compile(
    r"\b(labor\s+only|installation\s+services?|hourly\s+rate|man[- ]?hour|"
    r"scope\s+of\s+services?|professional\s+services)\b",
    re.I,
)
_PRODUCTISH = re.compile(
    r"\b(part|parts|equipment|supply|supplies|material|materials|sku|mpn|nsn|"
    r"model|oem|brand\s+or\s+equal|unit\s+price|bid\s+item|line\s+item)\b",
    re.I,
)
_LABOR_ROW = re.compile(r"\b(labor|install(?:ation)?|freight|shipping|delivery\s+only)\b", re.I)
_BOILER = re.compile(
    r"\b(terms\s+and\s+conditions|indemnif|insurance\s+requirements|page\s+\d+\s+of|"
    r"equal\s+opportunity|nondiscrimination)\b",
    re.I,
)


def _signal_score(text: str) -> dict[str, Any]:
    # Ignore near-empty text — short tokens like "set"/"box" must not fire on blank pages
    if not text or len(text.strip()) < 40:
        return {
            "signal_hits": [],
            "signal_count": 0,
            "has_qty_uom": False,
            "has_pn": False,
            "has_clin": False,
            "has_catalog_discount": False,
            "has_productish": False,
            "has_service_only": False,
        }
    low = f" {text.lower()} "
    hits = [t for t in SIGNAL_TERMS if t in low]
    return {
        "signal_hits": hits,
        "signal_count": len(hits),
        "has_qty_uom": bool(_QTY_UOM.search(text)),
        "has_pn": bool(_LABELED_PN.search(text)),
        "has_clin": bool(_CLIN.search(text)),
        "has_catalog_discount": bool(_CATALOG_DISCOUNT.search(text)),
        "has_productish": bool(_PRODUCTISH.search(text)),
        "has_service_only": bool(_SERVICE_ONLY.search(text)),
    }


def _pdf_pages(path: Path, *, max_pages: int = 30) -> list[dict[str, Any]]:
    """Extract page text only (fitz). Avoid table finders that can freeze the worker."""
    pages: list[dict[str, Any]] = []
    # Fast path: fitz text for all pages (avoids pdfplumber hangs on large packages)
    try:
        import fitz

        doc = fitz.open(str(path))
        try:
            limit = min(max_pages, doc.page_count)
            for i in range(limit):
                text = doc.load_page(i).get_text("text") or ""
                pages.append(
                    {
                        "page": i + 1,
                        "text": text,
                        "tables": [],
                        "word_count": len(text.split()),
                        "char_count": len(text),
                        "image_only": len(text.strip()) < 40,
                    }
                )
        finally:
            doc.close()
    except Exception:
        pages = []

    # Text-only path. Do NOT call page.find_tables() — it can hold the GIL and freeze
    # the whole worker so opp timeouts never fire.
    return pages


def _rows_from_table(table: dict[str, Any], *, page: int, path: Path) -> list[dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    raw = table.get("rows") or []
    if len(raw) < 2:
        return rows_out
    header = [c.lower() for c in raw[0]]
    header_blob = " | ".join(header)
    hs = _signal_score(header_blob)
    # Treat first row as header if it looks like one; else all rows are data.
    data_start = 1 if hs["signal_count"] >= 2 or any(
        k in header_blob for k in ("item", "qty", "desc", "unit", "price", "part", "clin")
    ) else 0
    for ri, row in enumerate(raw[data_start:], start=data_start + 1):
        cells = [c for c in row if c]
        if len(cells) < 2:
            continue
        blob = " | ".join(cells)
        if _BOILER.search(blob) or len(blob) < 8:
            continue
        if _LABOR_ROW.search(blob) and not _PRODUCTISH.search(blob) and not _QTY_UOM.search(blob):
            continue
        qm = _QTY_UOM.search(blob)
        pn = _LABELED_PN.search(blob)
        # Heuristic columns
        item = cells[0][:40] if cells else None
        desc = cells[1] if len(cells) > 1 else blob
        qty = None
        uom = None
        for c in cells:
            m = _QTY_UOM.search(c)
            if m:
                qty = m.group("qty")
                uom = m.group("uom")
                break
            if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", c.strip()):
                qty = c.strip()
        if not qty and not pn and not re.match(r"^\d{1,4}[A-Za-z]?$", str(item or "")):
            # Require at least item# + descriptive text for table rows without qty
            if len(desc) < 12:
                continue
        rows_out.append(
            {
                "item": item,
                "description": str(desc)[:500],
                "quantity": qty or (qm.group("qty") if qm else None),
                "uom": uom or (qm.group("uom") if qm else None),
                "part_number": pn.group(1) if pn else None,
                "equal_allowed": bool(re.search(r"\bor\s+equal\b", blob, re.I)),
                "_source_path": str(path),
                "_sheet": f"pdf_p{page}_t{table.get('table_index')}",
                "_page": page,
                "_row": ri,
                "_kind": "table_row",
            }
        )
    return rows_out


def _rows_from_text(text: str, *, page: int, path: Path, inherited_header: str | None) -> tuple[list[dict[str, Any]], str | None]:
    rows: list[dict[str, Any]] = []
    header = inherited_header
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 6 or _BOILER.search(line):
            continue
        low = line.lower()
        if sum(1 for t in ("item", "description", "qty", "quantity", "unit", "part") if t in low) >= 3:
            header = line
            continue
        if _LABOR_ROW.search(line) and not _QTY_UOM.search(line) and not _LABELED_PN.search(line):
            # Keep mixed product+labor: skip pure labor lines
            if not _PRODUCTISH.search(line):
                continue
        m = _ITEM_ROW.match(line) or _ITEM_ROW_LOOSE.match(line)
        if m:
            rows.append(
                {
                    "item": m.group("item"),
                    "description": m.group("desc").strip()[:500],
                    "quantity": m.group("qty"),
                    "uom": m.group("uom") if "uom" in m.groupdict() else None,
                    "part_number": (_LABELED_PN.search(line).group(1) if _LABELED_PN.search(line) else None),
                    "equal_allowed": bool(re.search(r"\bor\s+equal\b", line, re.I)),
                    "_source_path": str(path),
                    "_sheet": f"pdf_p{page}",
                    "_page": page,
                    "_header": header,
                    "_kind": "text_row",
                }
            )
            continue
        qm = _QTY_UOM.search(line)
        pn = _LABELED_PN.search(line)
        clin = _CLIN.search(line)
        if (qm and len(line) >= 18) or (pn and qm) or (clin and qm):
            rows.append(
                {
                    "item": clin.group(1) if clin else None,
                    "description": line[:500],
                    "quantity": qm.group("qty") if qm else None,
                    "uom": qm.group("uom") if qm else None,
                    "part_number": pn.group(1) if pn else None,
                    "equal_allowed": bool(re.search(r"\bor\s+equal\b", line, re.I)),
                    "_source_path": str(path),
                    "_sheet": f"pdf_p{page}",
                    "_page": page,
                    "_header": header,
                    "_kind": "signal_row",
                }
            )
            continue
        # Equipment datasheet / model lines: Model XYZ … without explicit qty
        if pn and len(line) >= 16 and _PRODUCTISH.search(line):
            rows.append(
                {
                    "item": None,
                    "description": line[:500],
                    "quantity": "1",
                    "uom": "EA",
                    "part_number": pn.group(1),
                    "equal_allowed": bool(re.search(r"\bor\s+equal\b", line, re.I)),
                    "_source_path": str(path),
                    "_sheet": f"pdf_p{page}",
                    "_page": page,
                    "_header": header,
                    "_kind": "model_row",
                }
            )
            continue
        if re.search(r"\b(model|part\s*number|mpn|sku)\b.+\b[A-Z0-9][A-Z0-9\-./]{2,}\b", line, re.I) and len(line) >= 20:
            if _BOILER.search(line) or _LABOR_ROW.search(line):
                continue
            rows.append(
                {
                    "item": None,
                    "description": line[:500],
                    "quantity": "1",
                    "uom": "EA",
                    "part_number": (_LABELED_PN.search(line).group(1) if _LABELED_PN.search(line) else None),
                    "equal_allowed": bool(re.search(r"\bor\s+equal\b", line, re.I)),
                    "_source_path": str(path),
                    "_sheet": f"pdf_p{page}",
                    "_page": page,
                    "_header": header,
                    "_kind": "equipment_row",
                }
            )
    return rows, header


def _classify_role(
    *,
    filename: str,
    signals: dict[str, Any],
    line_count: int,
    catalog_only: bool,
    product_pages: list[int],
) -> tuple[str, float]:
    name = (filename or "").lower()
    if catalog_only and line_count == 0:
        return "CATALOG_REFERENCE", 0.85
    if line_count >= 3 and signals["signal_count"] >= 4:
        if "spec" in name:
            return "SPECIFICATION_WITH_PRODUCT_TABLE", 0.9
        if any(x in name for x in ("invit", "solicit", "bid", "rfp", "rfq", "package")):
            return "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES", 0.88
        if any(x in name for x in ("price", "pricing", "schedule", "bid form", "line")):
            return "PRICING_SCHEDULE", 0.95
        return "PRODUCT_SCHEDULE", 0.9
    if line_count >= 1 and signals["has_productish"]:
        return "ITEM_LIST", 0.7
    if product_pages and signals["has_productish"]:
        return "SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES", 0.55
    if signals["has_service_only"] and not signals["has_productish"]:
        return "SERVICE_SCOPE", 0.8
    if line_count == 0 and not signals["has_productish"]:
        return "NON_PRODUCT_DOCUMENT", 0.6
    return "UNKNOWN", 0.3


def _parse_cache_load() -> dict[str, Any]:
    try:
        from m3_data_root import data_path

        path = data_path(_PARSE_CACHE_FILE)
        if path.exists():
            import json

            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"version": PARSER_CACHE_VERSION, "by_hash": {}}


def _parse_cache_save(cache: dict[str, Any]) -> None:
    try:
        from m3_data_root import data_path
        import json

        path = data_path(_PARSE_CACHE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Cap cache size
        by = cache.get("by_hash") or {}
        if len(by) > 400:
            # drop oldest half by inserted_at
            items = sorted(by.items(), key=lambda kv: str((kv[1] or {}).get("inserted_at") or ""))
            by = dict(items[len(items) // 2 :])
            cache["by_hash"] = by
        path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def inspect_document(path: str | Path, *, filename: str | None = None, max_pages: int = 30) -> dict[str, Any]:
    import hashlib

    path = Path(path)
    name = filename or path.name
    ext = path.suffix.lower().lstrip(".")
    # BidNet often saves as document_1 with no extension — sniff magic bytes
    if not ext and path.is_file():
        try:
            head = path.read_bytes()[:8]
            if head.startswith(b"%PDF"):
                ext = "pdf"
            elif head[:2] == b"PK":
                ext = "xlsx"
            elif head[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
                ext = "xls"
        except Exception:
            pass
    result: dict[str, Any] = {
        "build": BUILD,
        "path": str(path),
        "filename": name,
        "extension": ext,
        "document_role_content": "UNKNOWN",
        "role_confidence": 0.0,
        "product_signal_pages": [],
        "expected_product_lines": 0,
        "extracted_rows": [],
        "extraction_coverage": 0.0,
        "catalog_discount_only": False,
        "classification": "UNKNOWN",
        "operator_status": "",
        "page_diagnostics": [],
    }
    if not path.is_file() or path.stat().st_size < 32:
        result["classification"] = "PRODUCT_SCHEDULE_INACCESSIBLE"
        result["operator_status"] = "Document file is missing or empty."
        return result

    # Content-hash parse cache — skip reparse of unchanged documents
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    except Exception:
        digest = None
    if digest:
        cache = _parse_cache_load()
        if cache.get("version") == PARSER_CACHE_VERSION:
            hit = (cache.get("by_hash") or {}).get(digest)
            if isinstance(hit, dict) and hit.get("result"):
                cached = dict(hit["result"])
                cached["path"] = str(path)
                cached["filename"] = name
                cached["cache_hit"] = True
                cached["content_hash"] = digest
                return cached

    # Reject HTML viewer/login pages masquerading as attachments (never classify as no-product)
    try:
        from bidnet_engine.package_materialization import looks_like_html_bytes

        head = path.read_bytes()[:4096]
        if looks_like_html_bytes(head):
            result["classification"] = "PRODUCT_SCHEDULE_INACCESSIBLE"
            result["document_role_content"] = "UNKNOWN"
            result["operator_status"] = (
                "The downloaded file was a web page, not a bid attachment. "
                "M3 could not open the real product schedule yet."
            )
            result["signal_hits"] = []
            result["extracted_line_count"] = 0
            result["extracted_rows"] = []
            result["expected_product_lines"] = 0
            result["is_product_like"] = False
            return result
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    product_pages: list[int] = []
    agg_signals = {
        "signal_hits": [],
        "signal_count": 0,
        "has_qty_uom": False,
        "has_pn": False,
        "has_clin": False,
        "has_catalog_discount": False,
        "has_productish": False,
        "has_service_only": False,
    }

    if ext in {"xlsx", "xls", "csv"}:
        try:
            from product_identity.spreadsheet import extract_spreadsheet

            xrows = extract_spreadsheet(path) or []
            for r in xrows:
                if not isinstance(r, dict):
                    continue
                r.setdefault("_page", None)
                r.setdefault("_kind", "sheet_row")
                r.setdefault("_source_path", str(path))
                rows.append(r)
            if rows:
                product_pages = [1]
            blob = " ".join(str(r.get("description") or "") for r in rows[:40])
            sig = _signal_score(blob + " item qty unit part manufacturer")
            for k, v in sig.items():
                if k == "signal_hits":
                    agg_signals["signal_hits"] = list(set(agg_signals["signal_hits"] + v))
                    agg_signals["signal_count"] = len(agg_signals["signal_hits"])
                elif isinstance(v, bool):
                    agg_signals[k] = agg_signals.get(k) or v
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}:{exc}"[:160]
    elif ext == "pdf":
        pages = _pdf_pages(path, max_pages=max_pages)
        header: str | None = None
        # Cap OCR pages per document to avoid worker hangs (PyMuPDF OCR / tesseract)
        image_only_budget = 8
        for p in pages:
            page_no = int(p["page"])
            text = p.get("text") or ""
            sig = _signal_score(text)
            page_rows: list[dict[str, Any]] = []
            for table in p.get("tables") or []:
                page_rows.extend(_rows_from_table(table, page=page_no, path=path))
            text_rows, header = _rows_from_text(text, page=page_no, path=path, inherited_header=header)
            # Prefer table rows when present; still keep text rows not duplicated
            seen = {(r.get("description"), r.get("quantity"), r.get("item")) for r in page_rows}
            for r in text_rows:
                key = (r.get("description"), r.get("quantity"), r.get("item"))
                if key not in seen:
                    page_rows.append(r)
            # Bounded OCR only for truly image-only pages (NY BidNet scans often have zero native text)
            if p.get("image_only") and not page_rows and image_only_budget > 0:
                ocr_text = _ocr_page_fallback(path, page_no - 1)
                image_only_budget -= 1
                if ocr_text and len(ocr_text.strip()) >= 40:
                    text = ocr_text
                    sig = _signal_score(text)
                    page_rows, header = _rows_from_text(
                        text, page=page_no, path=path, inherited_header=header
                    )
                    p["ocr_used"] = True
                    p["text"] = text
                    p["char_count"] = len(text)
                    p["image_only"] = False
                else:
                    p["ocr_used"] = False
                    p["ocr_empty"] = True
            elif p.get("image_only") and not page_rows:
                p["ocr_used"] = False
                p["ocr_skipped"] = True
            product_like = (
                len(page_rows) >= 1
                or (sig["signal_count"] >= 4 and (sig["has_qty_uom"] or sig["has_pn"] or sig["has_clin"]))
                or (sig["has_productish"] and sig["has_qty_uom"] and sig["signal_count"] >= 2)
                or (sig["has_catalog_discount"] and sig["has_productish"])
            )
            if product_like:
                product_pages.append(page_no)
            rows.extend(page_rows)
            # pdfplumber table fallback when signals strong but text/fitz tables empty
            if not page_rows and (sig["has_qty_uom"] or sig["has_pn"] or sig["signal_count"] >= 5):
                try:
                    import pdfplumber

                    with pdfplumber.open(str(path)) as pdf:
                        if 0 <= page_no - 1 < len(pdf.pages):
                            for table in pdf.pages[page_no - 1].extract_tables() or []:
                                page_rows.extend(_rows_from_table(table, page=page_no, path=path))
                    if page_rows:
                        rows.extend(page_rows)
                        if page_no not in product_pages:
                            product_pages.append(page_no)
                except Exception:
                    pass
            result["page_diagnostics"].append(
                {
                    "page": page_no,
                    "signal_count": sig["signal_count"],
                    "rows": len(page_rows),
                    "image_only": bool(p.get("image_only")),
                    "ocr_used": bool(p.get("ocr_used")),
                    "ocr_skipped": bool(p.get("ocr_skipped")),
                    "ocr_empty": bool(p.get("ocr_empty")),
                    "char_count": p.get("char_count"),
                }
            )
            for k, v in sig.items():
                if k == "signal_hits":
                    agg_signals["signal_hits"] = list(set(agg_signals["signal_hits"] + v))[:40]
                    agg_signals["signal_count"] = len(agg_signals["signal_hits"])
                elif isinstance(v, bool):
                    agg_signals[k] = agg_signals.get(k) or v
    else:
        # plaintext / unknown — try read
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
            rows, _ = _rows_from_text(text, page=1, path=path, inherited_header=None)
            agg_signals = _signal_score(text)
            if rows:
                product_pages = [1]
        except Exception:
            pass

    # Deduplicate rows
    dedup: list[dict[str, Any]] = []
    seen_keys: set[tuple] = set()
    for r in rows:
        key = (
            str(r.get("item") or ""),
            str(r.get("description") or "")[:120],
            str(r.get("quantity") or ""),
            str(r.get("part_number") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        dedup.append(r)
    rows = dedup

    catalog_only = bool(agg_signals.get("has_catalog_discount")) and len(rows) < 2
    expected = max(len(rows), _estimate_expected(result.get("page_diagnostics") or [], rows, agg_signals))
    coverage = (len(rows) / expected) if expected else 0.0

    role, conf = _classify_role(
        filename=name,
        signals=agg_signals,
        line_count=len(rows),
        catalog_only=catalog_only,
        product_pages=product_pages,
    )

    # Title/filename hints for OEM parts RFQs that are often catalog-discount
    name_parts = bool(re.search(r"\b(parts?|oem|automotive|equipment)\b", name, re.I))
    if catalog_only or (agg_signals.get("has_catalog_discount") and len(rows) < 2):
        catalog_only = True
        classification = "CATALOG_DISCOUNT_ONLY"
        status = (
            "This solicitation asks for a discount from the manufacturer catalog "
            "rather than a fixed item list."
        )
    elif len(rows) >= 1 and (role in PRODUCT_ROLES or len(rows) >= 1):
        classification = "LINES_RECOVERED"
        pages_s = _pages_phrase(product_pages)
        status = f"{len(rows)} product lines found in {name}" + (f", {pages_s}." if pages_s else ".")
        role = role if role in PRODUCT_ROLES else "PRODUCT_SCHEDULE"
        conf = max(conf, 0.75)
    elif product_pages and len(rows) == 0:
        classification = "PARSER_DEFECT_REMAINS"
        status = (
            f"Product table signals detected on {_pages_phrase(product_pages)} of {name}, "
            "but line extraction did not recover rows. This deal is still being analyzed."
        )
    elif role == "SERVICE_SCOPE":
        classification = "NO_PRODUCT_LINES_ACTUALLY_PRESENT"
        status = "The available bid package describes services/labor rather than an itemized product list."
    elif agg_signals.get("has_productish") and name_parts and len(rows) == 0 and not product_pages:
        # Parts RFQ language without recoverable rows — likely catalog or parser gap
        if agg_signals.get("has_catalog_discount"):
            catalog_only = True
            classification = "CATALOG_DISCOUNT_ONLY"
            status = (
                "This solicitation asks for a discount from the manufacturer catalog "
                "rather than a fixed item list."
            )
        else:
            classification = "PARSER_DEFECT_REMAINS"
            status = (
                f"M3 found parts/equipment language in {name} but could not extract an item list yet. "
                "This deal is still being analyzed."
            )
    elif any(bool(d.get("image_only") or d.get("ocr_skipped") or d.get("ocr_empty")) for d in (result.get("page_diagnostics") or [])) and len(rows) == 0:
        classification = "PARSER_DEFECT_REMAINS"
        status = (
            f"{name} appears to be a scanned/image PDF with little extractable text. "
            "M3 is still analyzing this package for product lines."
        )
    elif role == "NON_PRODUCT_DOCUMENT" or (not product_pages and len(rows) == 0):
        classification = "NO_PRODUCT_LINES_ACTUALLY_PRESENT"
        status = "The available bid package does not contain an itemized product requirement."
    else:
        classification = "UNKNOWN"
        status = "Product data status is still being determined from the bid package."

    if expected > len(rows) >= 1 and coverage < 0.85:
        status = (
            f"Product table detected but extraction incomplete: {len(rows)} of {expected} rows."
        )

    result.update(
        {
            "document_role_content": role,
            "role_confidence": conf,
            "product_signal_pages": product_pages,
            "expected_product_lines": expected,
            "extracted_rows": rows,
            "extracted_line_count": len(rows),
            "extraction_coverage": round(coverage, 4),
            "catalog_discount_only": catalog_only,
            "classification": classification,
            "operator_status": status,
            "signals": {k: v for k, v in agg_signals.items() if k != "signal_hits"},
            "signal_hits": agg_signals.get("signal_hits") or [],
            "is_product_like": role in PRODUCT_ROLES or len(rows) >= 1,
            "authority_score": _authority_score(role, conf, name, len(rows), catalog_only),
        }
    )
    if digest:
        try:
            from application_clock import now_utc

            cache = _parse_cache_load()
            if cache.get("version") != PARSER_CACHE_VERSION:
                cache = {"version": PARSER_CACHE_VERSION, "by_hash": {}}
            slim = {
                k: result.get(k)
                for k in (
                    "build",
                    "extension",
                    "document_role_content",
                    "role_confidence",
                    "product_signal_pages",
                    "expected_product_lines",
                    "extracted_rows",
                    "extracted_line_count",
                    "extraction_coverage",
                    "catalog_discount_only",
                    "classification",
                    "operator_status",
                    "signals",
                    "signal_hits",
                    "is_product_like",
                    "authority_score",
                    "page_diagnostics",
                )
            }
            cache.setdefault("by_hash", {})[digest] = {
                "inserted_at": now_utc().isoformat(),
                "result": slim,
            }
            result["content_hash"] = digest
            result["cache_hit"] = False
            _parse_cache_save(cache)
        except Exception:
            pass
    return result


def _estimate_expected(page_diags: list[dict], rows: list[dict], signals: dict) -> int:
    if rows:
        # If diagnostics show more table rows than extracted, prefer that
        table_hint = sum(int(p.get("rows") or 0) for p in page_diags)
        return max(len(rows), table_hint if table_hint <= len(rows) * 3 else len(rows))
    if signals.get("has_clin") or signals.get("has_pn"):
        return 1
    return 0


def _authority_score(role: str, conf: float, filename: str, n_rows: int, catalog_only: bool) -> float:
    if catalog_only:
        return 0.2
    score = conf * 10 + min(n_rows, 50) * 0.15
    name = filename.lower()
    if "revis" in name or "final" in name or "updated" in name:
        score += 3
    if "addend" in name or "amend" in name:
        score += 2
    if role in {"PRICING_SCHEDULE", "PRODUCT_SCHEDULE", "BID_FORM"}:
        score += 4
    if role in {"SOLICITATION_WITH_EMBEDDED_PRODUCT_LINES", "SPECIFICATION_WITH_PRODUCT_TABLE", "ITEM_LIST"}:
        score += 3
    if name.endswith((".xlsx", ".xls", ".csv")):
        score += 2
    return score


def _pages_phrase(pages: list[int]) -> str:
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    return f"pages {pages[0]}–{pages[-1]}"


def _ocr_page_fallback(path: Path, page_index: int) -> str:
    """Single-page OCR fallback; used only when native text is empty."""
    try:
        import fitz

        doc = fitz.open(str(path))
        try:
            page = doc.load_page(page_index)
            # Try built-in OCR if available (PyMuPDF OCR)
            try:
                tp = page.get_textpage_ocr(dpi=200, full=True)  # type: ignore[attr-defined]
                return page.get_text("text", textpage=tp) or ""
            except Exception:
                pass
            # Rasterize + pytesseract if present
            try:
                import pytesseract
                from PIL import Image
                import io

                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                return pytesseract.image_to_string(img) or ""
            except Exception:
                return ""
        finally:
            doc.close()
    except Exception:
        return ""


def inspect_package_documents(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Inspect all local docs; pick authoritative product document; return rows."""
    inspections: list[dict[str, Any]] = []
    for d in docs:
        path = d.get("local_path") or d.get("LOCAL_PATH")
        if not path:
            continue
        insp = inspect_document(path, filename=str(d.get("filename") or d.get("document_name") or Path(path).name))
        insp["document_id"] = d.get("document_id") or d.get("SOURCE_DOCUMENT_ID")
        insp["source_url"] = d.get("source_url") or d.get("SOURCE_URL")
        inspections.append(insp)

    product_like = [i for i in inspections if i.get("is_product_like")]
    catalog_only = [i for i in inspections if i.get("catalog_discount_only")]
    ranked = sorted(inspections, key=lambda i: float(i.get("authority_score") or 0), reverse=True)
    auth = None
    for i in ranked:
        if i.get("is_product_like") and int(i.get("extracted_line_count") or 0) >= 1:
            auth = i
            break
    if not auth:
        for i in ranked:
            if i.get("is_product_like"):
                auth = i
                break

    all_rows: list[dict[str, Any]] = []
    if auth:
        all_rows = list(auth.get("extracted_rows") or [])
    else:
        for i in product_like:
            all_rows.extend(i.get("extracted_rows") or [])

    expected = int((auth or {}).get("expected_product_lines") or 0) or len(all_rows)
    extracted = len(all_rows)
    coverage = (extracted / expected) if expected else 0.0

    classification = "NO_PRODUCT_LINES_ACTUALLY_PRESENT"
    if auth and auth.get("catalog_discount_only"):
        classification = "CATALOG_DISCOUNT_ONLY"
    elif extracted >= 1:
        classification = "LINES_RECOVERED"
    elif any(i.get("classification") == "PARSER_DEFECT_REMAINS" for i in inspections):
        classification = "PARSER_DEFECT_REMAINS"
    elif catalog_only:
        classification = "CATALOG_DISCOUNT_ONLY"

    operator = (auth or {}).get("operator_status") or (
        "The available bid package does not contain an itemized product requirement."
    )
    if classification == "LINES_RECOVERED" and auth:
        pages = auth.get("product_signal_pages") or []
        operator = (
            f"{extracted} product lines found in {auth.get('filename')}"
            + (f", {_pages_phrase(pages)}." if pages else ".")
        )
        if expected > extracted and coverage < 0.85:
            operator = (
                f"Product table detected but extraction incomplete: {extracted} of {expected} rows."
            )

    return {
        "build": BUILD,
        "inspections": [
            {k: v for k, v in i.items() if k != "extracted_rows"}
            | {"extracted_line_count": i.get("extracted_line_count"), "sample_rows": (i.get("extracted_rows") or [])[:3]}
            for i in inspections
        ],
        "full_inspections": inspections,
        "product_like_documents": len(product_like),
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": bool(auth and (auth.get("extracted_line_count") or auth.get("is_product_like"))),
        "AUTHORITATIVE_PRODUCT_DOC": (
            {k: v for k, v in auth.items() if k != "extracted_rows"} if auth else None
        ),
        "authoritative_rows": all_rows,
        "EXPECTED_PRODUCT_LINES": expected,
        "EXTRACTED_PRODUCT_LINES": extracted,
        "LINE_EXTRACTION_COVERAGE": round(coverage, 4),
        "LINES_READY": extracted >= 1 and classification == "LINES_RECOVERED",
        "classification": classification,
        "operator_product_status": operator,
        "catalog_discount_only": classification == "CATALOG_DISCOUNT_ONLY",
    }
