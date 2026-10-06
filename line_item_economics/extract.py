"""Extract structured line-item tables from schedules, tables, and text."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from line_item_economics.models import OR_EQUAL_NO, OR_EQUAL_UNKNOWN, OR_EQUAL_YES, empty_line_item

_OR_EQUAL = re.compile(r"\b(or\s+equal|or\s+equivalent|brand\s+name\s+or\s+equal)\b", re.I)
_NO_SUBST = re.compile(r"\b(no\s+substitut|exact\s+match\s+only|brand\s+name\s+only|must\s+be)\b", re.I)
_NSN = re.compile(r"\b(\d{4}[- ]?\d{2}[- ]?\d{3}[- ]?\d{4})\b")
_PN = re.compile(r"\b(?:P/?N|MPN|PART\s*#?|MODEL)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,})\b", re.I)
_MFG = re.compile(r"\b(?:MFR|MFG|MANUFACTURER|BRAND)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9 &\-.]{1,40})\b", re.I)
_QTY = re.compile(r"\b(?:QTY|QUANTITY)\s*[:#]?\s*(\d[\d,]*(?:\.\d+)?)\b", re.I)
_UOM = re.compile(r"\b(EA|EACH|CS|CASE|BX|BOX|PK|PACK|DOZEN|DZ|FT|LF|GAL|LB|SET|KIT|ROLL|PALLET)\b", re.I)
_CLIN = re.compile(r"\b(?:CLIN|ITEM|LINE)\s*[#:]?\s*([0-9A-Z]{1,10})\b", re.I)
_PACK = re.compile(r"\b(?:case|box|pack|pkg)\s*(?:of|\/)\s*(\d{1,5})\b", re.I)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _or_equal_from_text(text: str | None) -> str:
    t = text or ""
    if _NO_SUBST.search(t):
        return OR_EQUAL_NO
    if _OR_EQUAL.search(t):
        return OR_EQUAL_YES
    return OR_EQUAL_UNKNOWN


def _pick(row: dict[str, Any], *keys: str) -> Any:
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lower and lower[key.lower()] not in (None, ""):
            return lower[key.lower()]
    return None


def line_from_row(row: dict[str, Any], *, index: int, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one schedule/table row into a line item. Does not invent fields."""
    desc = _str(_pick(row, "product_description", "description", "desc", "item_description", "item", "product", "title"))
    clin = _str(_pick(row, "clin", "line_number", "line", "item_number", "item_no", "lineno"))
    qty = _num(_pick(row, "quantity", "qty", "qnty", "qty_ordered"))
    uom = _str(_pick(row, "unit_of_measure", "uom", "unit", "units"))
    pack = _num(_pick(row, "pack_size", "pack", "case_pack", "units_per_case"))
    if pack is None and desc:
        m = _PACK.search(desc)
        if m:
            pack = float(m.group(1))
    brand = _str(_pick(row, "brand", "requested_brand", "manufacturer_brand"))
    mfr = _str(_pick(row, "manufacturer", "mfr", "mfg", "vendor"))
    model = _str(_pick(row, "model", "model_number", "model_no"))
    pn = _str(_pick(row, "part_number", "pn", "mpn", "sku", "catalog_number"))
    nsn = _str(_pick(row, "nsn", "national_stock_number"))
    notes = _str(_pick(row, "line_notes", "notes", "remarks", "comment"))
    text_blob = " ".join(str(x) for x in (desc, notes, brand, mfr) if x)
    or_eq = _str(_pick(row, "or_equal_allowed", "or_equal", "brand_or_equal"))
    if or_eq:
        ou = or_eq.strip().upper()
        if ou in {"Y", "YES", "TRUE", "1", "ALLOWED"}:
            or_eq = OR_EQUAL_YES
        elif ou in {"N", "NO", "FALSE", "0", "NOT ALLOWED"}:
            or_eq = OR_EQUAL_NO
        else:
            or_eq = OR_EQUAL_UNKNOWN
    else:
        or_eq = _or_equal_from_text(text_blob)

    line = empty_line_item(
        line_id=f"L{index:04d}",
        clin=clin,
        line_number=clin or str(index),
        product_description=desc,
        manufacturer=mfr,
        brand=brand,
        model=model,
        part_number=pn,
        nsn=nsn,
        upc=_str(_pick(row, "upc", "ean", "gtin")),
        size=_str(_pick(row, "size")),
        color=_str(_pick(row, "color", "colour")),
        material=_str(_pick(row, "material")),
        pack_size=pack,
        unit_of_measure=uom.upper() if uom else None,
        quantity=qty,
        requested_brand=brand or _str(_pick(row, "requested_brand")),
        or_equal_allowed=or_eq,
        salient_characteristics=_str(_pick(row, "salient_characteristics", "salient", "specs", "specification")),
        delivery_location=_str(_pick(row, "delivery_location", "ship_to", "location", "destination")),
        delivery_date=_str(_pick(row, "delivery_date", "required_delivery", "due_date")),
        line_notes=notes,
        original_text=_str(_pick(row, "original_text")) or desc,
        provenance=[provenance] if provenance else [{"source": "schedule_row", "row_index": index}],
    )
    # Fill identity hints from description when structured fields absent
    if desc:
        if not line["nsn"]:
            m = _NSN.search(desc)
            if m:
                line["nsn"] = m.group(1).replace(" ", "-")
        if not line["part_number"]:
            m = _PN.search(desc)
            if m:
                line["part_number"] = m.group(1)
        if not line["manufacturer"]:
            m = _MFG.search(desc)
            if m:
                line["manufacturer"] = m.group(1).strip()
        if line["quantity"] is None:
            m = _QTY.search(desc)
            if m:
                line["quantity"] = _num(m.group(1))
        if not line["unit_of_measure"]:
            m = _UOM.search(desc)
            if m:
                line["unit_of_measure"] = m.group(1).upper()
    return line


def extract_from_schedule_rows(rows: list[dict[str, Any]], *, source: str = "bid_schedule") -> list[dict[str, Any]]:
    """Prefer bid-schedule / pricing-sheet rows when present."""
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        # Skip empty rows
        if not any(v not in (None, "") for v in row.values()):
            continue
        out.append(
            line_from_row(
                row,
                index=i,
                provenance={"source": source, "row_index": i, "extractor": "schedule"},
            )
        )
    return out


def extract_from_csv_text(text: str, *, source: str = "csv_schedule") -> list[dict[str, Any]]:
    if not text or not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(r) for r in reader]
    return extract_from_schedule_rows(rows, source=source)


def extract_from_text(text: str, *, source: str = "solicitation_body") -> list[dict[str, Any]]:
    """Heuristic CLIN/qty extraction when no schedule exists. Never invents qty."""
    if not text:
        return []
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Split into candidate lines
    chunks = re.split(r"[\n\r]+", text)
    idx = 0
    for chunk in chunks:
        c = chunk.strip()
        if len(c) < 8:
            continue
        clin_m = _CLIN.search(c)
        qty_m = _QTY.search(c) or re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*(EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF)\b", c, re.I)
        if not clin_m and not qty_m:
            continue
        idx += 1
        clin = clin_m.group(1) if clin_m else str(idx)
        qty = None
        uom = None
        if qty_m:
            qty = _num(qty_m.group(1))
            if qty_m.lastindex and qty_m.lastindex >= 2:
                uom = qty_m.group(2)
        key = f"{clin}|{qty}|{c[:60]}"
        if key in seen:
            continue
        seen.add(key)
        row = {
            "clin": clin,
            "description": c[:500],
            "quantity": qty,
            "uom": uom,
            "original_text": c,
        }
        lines.append(
            line_from_row(
                row,
                index=idx,
                provenance={"source": source, "extractor": "text_heuristic", "excerpt": c[:200]},
            )
        )
        if len(lines) >= 500:
            break
    return lines


def extract_line_items(
    *,
    schedule_rows: list[dict[str, Any]] | None = None,
    csv_text: str | None = None,
    body_text: str | None = None,
    existing_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract line items preferring schedule > CSV > existing > body text."""
    if existing_lines:
        normalized = []
        for i, row in enumerate(existing_lines, start=1):
            if row.get("kind") == "LineItemEconomicsLine" and row.get("line_id"):
                normalized.append(row)
            else:
                normalized.append(line_from_row(row, index=i, provenance={"source": "existing_lines"}))
        return {
            "source_used": "existing_lines",
            "line_count": len(normalized),
            "lines": normalized,
        }
    if schedule_rows:
        lines = extract_from_schedule_rows(schedule_rows, source="bid_schedule")
        return {"source_used": "bid_schedule", "line_count": len(lines), "lines": lines}
    if csv_text:
        lines = extract_from_csv_text(csv_text)
        return {"source_used": "csv_schedule", "line_count": len(lines), "lines": lines}
    if body_text:
        lines = extract_from_text(body_text)
        return {"source_used": "solicitation_body", "line_count": len(lines), "lines": lines}
    return {"source_used": None, "line_count": 0, "lines": []}
