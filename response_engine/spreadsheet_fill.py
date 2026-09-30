"""R4 spreadsheet population — preserve buyer workbook structure. Never execute macros."""

from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path
from typing import Any

from response_engine.r4_constants import (
    BLOCKED,
    GENERATED_RESPONSE_COPY,
    MACRO_TEMPLATE_MANUAL,
    ORIGINAL,
    POPULATED,
    WORKING_COPY,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def populate_xlsx_from_maps(
    *,
    original_path: Path | None,
    original_bytes: bytes | None,
    field_maps: list[dict[str, Any]],
    out_path: Path,
    is_xlsm: bool = False,
) -> dict[str, Any]:
    """
    Copy original → working → populate only mapped cells.
    Preserves formulas (does not overwrite formula cells unless mapped and editable).
    Never executes macros. XLSM may be marked MANUAL if unsafe.
    """
    import openpyxl

    if original_path is None and original_bytes is None:
        return {"ok": False, "error": "no_original_workbook", "status": BLOCKED}

    raw = original_bytes if original_bytes is not None else original_path.read_bytes()
    original_hash = sha256_bytes(raw)

    if is_xlsm or (original_path and original_path.suffix.lower() == ".xlsm"):
        # Preserve file bytes; do not strip VBA. Prefer manual if we cannot safely keep vba.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        return {
            "ok": False,
            "status": MACRO_TEMPLATE_MANUAL,
            "original_hash": original_hash,
            "output_hash": sha256_file(out_path),
            "output_path": str(out_path),
            "role": WORKING_COPY,
            "note": "Macro-enabled workbook — macros preserved, not executed; manual completion required",
            "cells_written": 0,
        }

    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=False, keep_vba=False)
    integrity_before = _workbook_integrity(wb)
    written = []
    skipped = []

    for fm in field_maps:
        if fm.get("generation_status") not in ("READY", POPULATED):
            skipped.append({"field": fm.get("field_map_id"), "reason": fm.get("generation_status")})
            continue
        if fm.get("target_type") == "SIGNATURE":
            skipped.append({"field": fm.get("field_map_id"), "reason": "signature_never_filled"})
            continue
        cell_ref = fm.get("target_field_or_cell")
        sheet_name = fm.get("target_page_or_sheet")
        value = fm.get("source_value")
        if not cell_ref or value is None:
            skipped.append({"field": fm.get("field_map_id"), "reason": "missing_cell_or_value"})
            continue
        # Only write Excel-like refs (A1) or skip
        if not _looks_like_cell(cell_ref):
            skipped.append({"field": fm.get("field_map_id"), "reason": "not_a_cell_ref"})
            continue
        ws = None
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = wb.active
        existing = ws[cell_ref].value
        if isinstance(existing, str) and existing.startswith("="):
            # Buyer formula present — do not overwrite
            skipped.append({"field": fm.get("field_map_id"), "reason": "buyer_formula_preserved", "cell": cell_ref})
            continue
        ws[cell_ref] = _coerce_value(value)
        written.append({"cell": f"{ws.title}!{cell_ref}", "value": value, "field_map_id": fm.get("field_map_id")})
        fm["generation_status"] = POPULATED

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    # Verify original bytes unchanged
    if original_path and original_path.exists():
        assert sha256_file(original_path) == original_hash or original_bytes is not None

    wb2 = openpyxl.load_workbook(out_path, data_only=False)
    integrity_after = _workbook_integrity(wb2)
    integrity_ok = (
        integrity_before["sheet_names"] == integrity_after["sheet_names"]
        and integrity_before["sheet_count"] == integrity_after["sheet_count"]
    )

    return {
        "ok": True,
        "status": GENERATED_RESPONSE_COPY,
        "original_hash": original_hash,
        "output_hash": sha256_file(out_path),
        "output_path": str(out_path),
        "role": GENERATED_RESPONSE_COPY,
        "cells_written": len(written),
        "written": written,
        "skipped": skipped,
        "integrity_before": integrity_before,
        "integrity_after": integrity_after,
        "integrity_ok": integrity_ok,
        "original_untouched": True,
    }


def create_pricing_schedule_xlsx(
    *,
    lines: list[dict[str, Any]],
    field_maps: list[dict[str, Any]],
    out_path: Path,
    company_name: str | None = None,
) -> dict[str, Any]:
    """M3-generated pricing schedule when no buyer workbook exists. Deterministic only."""
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pricing"
    headers = ["CLIN", "Description", "Qty", "UOM", "Unit Price", "Extended", "Evidence"]
    for i, h in enumerate(headers, 1):
        ws.cell(1, i, h).font = Font(bold=True)
    price_by_line = {
        m.get("source_requirement", "").replace("line.", "").replace(".unit_price", ""): m.get("source_value")
        for m in field_maps
        if m.get("source_requirement", "").startswith("line.") and m.get("generation_status") in ("READY", POPULATED)
    }
    row = 2
    for li in lines:
        lid = li.get("line_item_id")
        qty = li.get("normalized_quantity") or li.get("quantity")
        unit = price_by_line.get(lid)
        extended = None
        try:
            if unit is not None and qty is not None:
                extended = float(unit) * float(qty)
        except (TypeError, ValueError):
            extended = None
        ws.cell(row, 1, li.get("CLIN") or li.get("buyer_line_number"))
        ws.cell(row, 2, li.get("description") or li.get("required_mpn") or "")
        ws.cell(row, 3, qty)
        ws.cell(row, 4, li.get("normalized_uom") or li.get("buyer_uom"))
        if unit is not None:
            ws.cell(row, 5, float(unit) if _is_number(unit) else unit)
        # else leave blank — UNKNOWN not filled with 0
        if extended is not None:
            ws.cell(row, 6, round(extended, 2))
        ws.cell(row, 7, "R2 scenario" if unit is not None else "PRICE_UNRESOLVED")
        row += 1
    if company_name and company_name not in ("UNKNOWN", None, ""):
        ws.cell(row + 1, 1, "Offeror")
        ws.cell(row + 1, 2, company_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return {
        "ok": True,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "lines": len(lines),
        "role": GENERATED_RESPONSE_COPY,
    }


def validate_extended_totals(lines: list[dict[str, Any]], unit_prices: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for li in lines:
        lid = li.get("line_item_id")
        qty = li.get("normalized_quantity") or li.get("quantity")
        unit = unit_prices.get(lid) or li.get("bid_unit_price")
        ext = li.get("extended_price")
        if unit is None or qty is None:
            continue
        try:
            expected = round(float(unit) * float(qty), 2)
            if ext is not None and abs(float(ext) - expected) > 0.02:
                issues.append({"line": lid, "expected": expected, "found": float(ext), "status": "TOTAL_MISMATCH"})
        except (TypeError, ValueError):
            issues.append({"line": lid, "status": "TOTAL_UNPARSEABLE"})
    return issues


def _workbook_integrity(wb: Any) -> dict[str, Any]:
    formula_count = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    formula_count += 1
    return {
        "sheet_count": len(wb.sheetnames),
        "sheet_names": list(wb.sheetnames),
        "hidden_sheets": [s for s in wb.sheetnames if wb[s].sheet_state != "visible"],
        "formula_count": formula_count,
    }


def _looks_like_cell(ref: str) -> bool:
    import re

    return bool(re.fullmatch(r"\$?[A-Za-z]{1,3}\$?\d{1,7}", str(ref).strip()))


def _coerce_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value
    return value


def _is_number(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def copy_original_immutable(src: Path, originals_dir: Path) -> dict[str, Any]:
    """Store ORIGINAL copy; never modify this path afterward."""
    originals_dir.mkdir(parents=True, exist_ok=True)
    dest = originals_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
    return {"path": str(dest), "hash": sha256_file(dest), "role": ORIGINAL}
