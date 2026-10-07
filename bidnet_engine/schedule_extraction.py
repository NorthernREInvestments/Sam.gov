"""Document inventory + prioritized authoritative schedule extraction for BidNet packages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from application_clock import now_utc

_SCHEDULE_NAME = re.compile(
    r"(price|pricing|bid\s*sheet|bid\s*schedule|cost\s*proposal|schedule|"
    r"line[\-\s]?item|item\s*list|material\s*list|bill\s*of\s*materials|\bbom\b|"
    r"equipment\s*list|supply\s*list|quote\s*sheet|itemized|unit\s*price|parts\s*list)",
    re.I,
)
_SKIP_NAME = re.compile(
    r"(terms\s*and\s*conditions|w-?9|insurance|signature|instructions?\b|"
    r"affidavit|certification|bond\s*form)",
    re.I,
)
_BOM = re.compile(r"\b(bom|bill\s*of\s*materials|material\s*list)\b", re.I)
_BID_FORM = re.compile(r"\b(bid\s*form|itemized\s*bid|proposal\s*form)\b", re.I)

ROLE_PRIORITY = {
    "PRICING_SCHEDULE": 100,
    "BID_FORM": 90,
    "LINE_ITEM_SCHEDULE": 85,
    "BOM": 80,
    "PRIMARY_SOLICITATION": 40,
    "SPECIFICATION": 30,
    "AMENDMENT": 20,
    "ATTACHMENT": 10,
    "OTHER": 5,
}


def classify_document_role(name: str, url: str = "", document_type: str = "") -> str:
    blob = f"{name} {url} {document_type}".lower()
    if "amend" in blob or "addendum" in blob:
        return "AMENDMENT"
    if _BOM.search(blob):
        return "BOM"
    if _BID_FORM.search(blob) or document_type == "bid_form":
        return "BID_FORM"
    if any(k in blob for k in ("pricing", "price schedule", "price sheet", "unit price", "xlsx", "xls", "csv")):
        return "PRICING_SCHEDULE"
    if any(k in blob for k in ("line item", "item list", "equipment list", "supply list", "bid schedule")):
        return "LINE_ITEM_SCHEDULE"
    if document_type == "pricing_sheet":
        return "PRICING_SCHEDULE"
    if any(k in blob for k in ("spec", "scope", "technical")):
        return "SPECIFICATION"
    if blob.endswith(".pdf") or "solicitation" in blob or "ifb" in blob or "itb" in blob or "rfq" in blob:
        return "PRIMARY_SOLICITATION"
    if _SCHEDULE_NAME.search(blob):
        return "LINE_ITEM_SCHEDULE"
    return "ATTACHMENT"


def build_document_inventory(docs: list[dict[str, Any]], *, opportunity_id: str = "") -> list[dict[str, Any]]:
    """DOCUMENT_INVENTORY for every package document."""
    out: list[dict[str, Any]] = []
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("document_name") or d.get("filename") or d.get("name") or "")
        url = str(d.get("document_url") or d.get("url") or d.get("source_url") or "")
        path_s = d.get("local_path")
        path = Path(path_s) if path_s else None
        ext = ""
        size = d.get("size_bytes")
        sheet_names: list[str] = []
        page_count = None
        text_extractable = False
        table_likely = False
        exists = bool(path and path.exists())
        if exists and path:
            ext = path.suffix.lower()
            try:
                size = size or path.stat().st_size
            except Exception:
                pass
            if ext in {".xlsx", ".xls", ".csv"}:
                text_extractable = True
                table_likely = True
                if ext == ".xlsx":
                    try:
                        import openpyxl

                        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                        sheet_names = list(wb.sheetnames)
                        wb.close()
                    except Exception:
                        sheet_names = []
            elif ext in {".docx", ".doc"}:
                text_extractable = True
                table_likely = True
            elif ext == ".pdf":
                text_extractable = True
                table_likely = bool(_SCHEDULE_NAME.search(name))
            elif ext in {".txt", ".html", ".htm"}:
                text_extractable = True
        role = classify_document_role(name, url, str(d.get("document_type") or ""))
        pricing_likely = role in {"PRICING_SCHEDULE", "BID_FORM", "LINE_ITEM_SCHEDULE", "BOM"}
        line_schedule_likely = pricing_likely or bool(_SCHEDULE_NAME.search(name))
        out.append(
            {
                "opportunity_id": opportunity_id,
                "filename": name,
                "document_type": d.get("document_type"),
                "document_role": role,
                "extension": ext or Path(name).suffix.lower(),
                "size": size,
                "page_count": page_count,
                "sheet_names": sheet_names,
                "text_extractable": text_extractable,
                "table_likely": table_likely,
                "pricing_likely": pricing_likely,
                "line_schedule_likely": line_schedule_likely,
                "amendment_status": role == "AMENDMENT",
                "source_url": url or None,
                "hash": d.get("content_hash"),
                "local_path": str(path) if exists else path_s,
                "retrieval_status": d.get("retrieval_status"),
                "exists_local": exists,
            }
        )
    # Sort by extraction priority
    out.sort(key=lambda x: (-ROLE_PRIORITY.get(str(x.get("document_role")), 0), str(x.get("filename") or "")))
    return out


def line_extraction_coverage_class(extracted: int, expected: int | None) -> str:
    if expected is None or expected <= 0:
        if extracted <= 0:
            return "FAILED"
        return "USABLE"  # no expected baseline — provisional
    pct = 100.0 * extracted / expected
    if pct < 50:
        return "FAILED"
    if pct < 80:
        return "PARTIAL"
    if pct < 95:
        return "USABLE"
    if pct < 100:
        return "STRONG"
    return "COMPLETE"


def _rows_to_schedule_rows(raw: list[dict[str, Any]], *, doc: dict[str, Any], parser: str) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            continue
        row = {
            "description": r.get("description") or r.get("product_description") or r.get("item"),
            "item_number": r.get("item") or r.get("item_number") or r.get("line_number") or r.get("CLIN_or_item_number"),
            "line_number": r.get("line_number") or r.get("item") or str(i + 1),
            "manufacturer": r.get("manufacturer") or r.get("mfr"),
            "brand": r.get("brand") or r.get("manufacturer"),
            "model": r.get("model"),
            "part_number": r.get("part_number") or r.get("mpn") or r.get("catalog_number"),
            "sku": r.get("sku"),
            "nsn": r.get("nsn"),
            "quantity": r.get("quantity") or r.get("qty"),
            "uom": r.get("uom") or r.get("unit_of_measure"),
            "pack": r.get("pack") or r.get("pack_size"),
            "or_equal": r.get("or_equal") or r.get("brand_or_equal"),
            "original_text": r.get("original_text") or r.get("description"),
            "_source_path": r.get("_source_path") or doc.get("local_path"),
            "_sheet": r.get("_sheet") or r.get("source_sheet"),
            "_row": r.get("_row") or r.get("source_row") or (i + 1),
            "_page": r.get("_page") or r.get("source_page"),
            "_parser": parser,
            "_document_role": doc.get("document_role"),
            "_filename": doc.get("filename"),
        }
        # Drop empty noise
        if not any(row.get(k) for k in ("description", "part_number", "model", "nsn", "sku")):
            continue
        out.append(row)
    return out


def _extract_one_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Try primary + fallbacks for one inventoried document."""
    path_s = doc.get("local_path")
    path = Path(path_s) if path_s else None
    role = str(doc.get("document_role") or "")
    name = str(doc.get("filename") or "")
    result: dict[str, Any] = {
        "document": name,
        "role": role,
        "format": doc.get("extension"),
        "parsers_tried": [],
        "rows": [],
        "ok": False,
        "error": None,
        "fallback_used": None,
    }
    if not path or not path.exists():
        result["error"] = "local_file_missing"
        return result
    if _SKIP_NAME.search(name) and not _SCHEDULE_NAME.search(name):
        result["error"] = "skipped_non_schedule_doc"
        return result

    ext = path.suffix.lower()
    rows: list[dict[str, Any]] = []

    # 1) Spreadsheet direct
    if ext in {".xlsx", ".xls", ".csv"}:
        result["parsers_tried"].append("product_identity.spreadsheet.extract_spreadsheet")
        try:
            from product_identity.spreadsheet import extract_spreadsheet

            raw = extract_spreadsheet(path)
            rows = _rows_to_schedule_rows(raw, doc=doc, parser="extract_spreadsheet")
        except Exception as exc:
            result["error"] = f"spreadsheet:{type(exc).__name__}:{exc}"[:200]
        if not rows:
            # fallback: table_extractors on bytes
            result["parsers_tried"].append("table_extractors.extract_xlsx/csv")
            try:
                data = path.read_bytes()
                if ext == ".csv":
                    from table_extractors import extract_csv_table

                    tab = extract_csv_table(data, source_name=name)
                elif ext == ".xlsx":
                    from table_extractors import extract_xlsx_table

                    tab = extract_xlsx_table(data, source_name=name)
                else:
                    from table_extractors import extract_xls_table

                    tab = extract_xls_table(data, source_name=name)
                items = tab.get("line_items") or []
                rows = _rows_to_schedule_rows(items, doc=doc, parser=str(tab.get("extraction_method") or "table_extractors"))
                if rows:
                    result["fallback_used"] = "table_extractors"
            except Exception as exc:
                result["error"] = (result.get("error") or "") + f"|table:{type(exc).__name__}"

    elif ext == ".docx":
        result["parsers_tried"].append("table_extractors.extract_docx_tables")
        try:
            data = path.read_bytes()
            from table_extractors import extract_docx_tables

            tab = extract_docx_tables(data, source_name=name)
            items = tab.get("line_items") or []
            rows = _rows_to_schedule_rows(items, doc=doc, parser="docx_tables")
        except Exception as exc:
            result["error"] = f"docx:{type(exc).__name__}:{exc}"[:200]
        if not rows:
            result["parsers_tried"].append("file_mining_text_blocks")
            try:
                from eligibility_and_recovery.file_mining import extract_document_pages
                from line_item_economics.extract import extract_from_text

                pages = extract_document_pages(path) or []
                text = "\n".join(str(p.get("text") or p) for p in pages[:40] if isinstance(p, dict))
                lines = extract_from_text(text) or []
                rows = _rows_to_schedule_rows(lines, doc=doc, parser="docx_text_blocks")
                if rows:
                    result["fallback_used"] = "text_block_row_reconstruction"
            except Exception as exc:
                result["error"] = (result.get("error") or "") + f"|text:{type(exc).__name__}"

    elif ext == ".pdf":
        result["parsers_tried"].append("product_identity.pdf_extract.extract_pdf_rows")
        pdf_text = ""
        try:
            from product_identity.pdf_extract import extract_pdf_rows

            pricing, _specs = extract_pdf_rows(path)
            rows = _rows_to_schedule_rows(pricing, doc=doc, parser="extract_pdf_rows")
        except Exception as exc:
            result["error"] = f"pdf:{type(exc).__name__}:{exc}"[:200]
        if not rows:
            result["parsers_tried"].append("table_extractors.extract_pdf_tables_heuristic")
            try:
                from document_quality import extract_pdf_text
                from table_extractors import extract_pdf_tables_heuristic

                pdf_text = extract_pdf_text(str(path), max_pages=40) or ""
                tab = extract_pdf_tables_heuristic(pdf_text, source_name=name)
                items = tab.get("line_items") or []
                rows = _rows_to_schedule_rows(items, doc=doc, parser="pdf_tables_heuristic")
                if rows:
                    result["fallback_used"] = "pdf_tables_heuristic"
            except Exception as exc:
                result["error"] = (result.get("error") or "") + f"|pdf_table:{type(exc).__name__}"
        if not rows:
            result["parsers_tried"].append("pdf_text_blocks")
            try:
                from document_quality import extract_pdf_text
                from line_item_economics.extract import extract_from_text

                if not pdf_text:
                    pdf_text = extract_pdf_text(str(path), max_pages=40) or ""
                lines = extract_from_text(pdf_text) or []
                rows = _rows_to_schedule_rows(lines, doc=doc, parser="pdf_text_blocks")
                if rows:
                    result["fallback_used"] = "text_block_row_reconstruction"
            except Exception as exc:
                result["error"] = (result.get("error") or "") + f"|pdf_text:{type(exc).__name__}"

    else:
        result["error"] = f"unsupported_format:{ext}"

    result["rows"] = rows
    result["ok"] = len(rows) > 0
    if not rows and result.get("error") is None:
        result["error"] = "parser_returned_zero_rows"
    return result


def estimate_expected_lines(rows: list[dict[str, Any]], inventory: list[dict[str, Any]], body_text: str = "") -> int | None:
    """Best-effort expected product line count from evidence — None if unknown."""
    if rows:
        # If we already extracted, expected at least that many unless filename hints higher
        base = len(rows)
    else:
        base = 0
    hint = None
    blob = body_text or ""
    for doc in inventory:
        blob += " " + str(doc.get("filename") or "")
    nums = re.findall(r"\b(?:item|line|clin)\s*[#:]?\s*(\d{1,4})\b", blob, re.I)
    if nums:
        try:
            hint = max(int(x) for x in nums)
        except ValueError:
            hint = None
    if hint and hint >= base:
        return hint
    if base > 0:
        return base
    return hint


def extract_schedules_from_package(
    attachments: list[dict[str, Any]],
    *,
    opportunity_id: str = "",
    body_text: str = "",
    gate_expected: int | None = None,
) -> dict[str, Any]:
    """
    Inventory docs, extract in priority order, record SCHEDULE_PRESENT_EXTRACTION_ZERO.
    Returns schedule_rows ready for analyze_line_item_economics.
    """
    inventory = build_document_inventory(attachments, opportunity_id=opportunity_id)
    schedule_roles = {"PRICING_SCHEDULE", "LINE_ITEM_SCHEDULE", "BID_FORM", "BOM"}
    schedule_docs = [d for d in inventory if d.get("document_role") in schedule_roles or d.get("line_schedule_likely")]
    # Always try high-priority docs first; then other extractable docs
    ordered = list(inventory)

    all_rows: list[dict[str, Any]] = []
    per_doc: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    parsers_used: list[str] = []

    for doc in ordered:
        if not doc.get("exists_local"):
            continue
        # Prefer schedule-likely; still try solicitation PDF if nothing yet
        if doc.get("document_role") not in schedule_roles and not doc.get("line_schedule_likely"):
            if all_rows:
                continue
            if doc.get("document_role") not in {"PRIMARY_SOLICITATION", "SPECIFICATION", "ATTACHMENT"}:
                continue
        extracted = _extract_one_document(doc)
        per_doc.append(extracted)
        parsers_used.extend(extracted.get("parsers_tried") or [])
        rows = extracted.get("rows") or []
        if rows:
            all_rows.extend(rows)
        elif doc.get("document_role") in schedule_roles and doc.get("exists_local"):
            hard_failures.append(
                {
                    "opportunity_id": opportunity_id,
                    "document": doc.get("filename"),
                    "format": doc.get("extension"),
                    "role": doc.get("document_role"),
                    "expected_lines": gate_expected,
                    "parser": (extracted.get("parsers_tried") or ["none"])[-1],
                    "parsers_tried": extracted.get("parsers_tried"),
                    "failure": extracted.get("error") or "SCHEDULE_PRESENT_EXTRACTION_ZERO",
                    "fallback_result": extracted.get("fallback_used") or "none_succeeded",
                    "code": "SCHEDULE_PRESENT_EXTRACTION_ZERO",
                }
            )

    # Dedup by description+qty+pn
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in all_rows:
        key = "|".join(
            str(r.get(k) or "").strip().lower()
            for k in ("description", "part_number", "quantity", "model", "nsn")
        )
        if key in seen or key == "||||":
            continue
        seen.add(key)
        deduped.append(r)

    expected = gate_expected or estimate_expected_lines(deduped, inventory, body_text)
    coverage_pct = None
    if expected and expected > 0:
        coverage_pct = round(100.0 * len(deduped) / expected, 1)
    coverage_class = line_extraction_coverage_class(len(deduped), expected)

    return {
        "DOCUMENT_INVENTORY": inventory,
        "schedule_docs_found": len(schedule_docs),
        "schedule_roles_present": sorted({str(d.get("document_role")) for d in schedule_docs}),
        "schedule_rows": deduped,
        "EXTRACTED_PRODUCT_LINES": len(deduped),
        "EXPECTED_PRODUCT_LINES": expected,
        "LINE_EXTRACTION_COVERAGE": coverage_pct,
        "LINE_EXTRACTION_COVERAGE_CLASS": coverage_class,
        "SCHEDULE_PRESENT_EXTRACTION_ZERO": hard_failures,
        "per_document_extraction": per_doc,
        "parsers_used": sorted(set(parsers_used)),
        "extracted_at": now_utc().isoformat(),
        "source_priority_applied": True,
    }
