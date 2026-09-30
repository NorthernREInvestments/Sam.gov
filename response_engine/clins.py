"""R2 CLIN / line-item extraction — preserve buyer structure + provenance."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import empty_provenance, new_id
from response_engine.r2_constants import (
    LINE_ITEM,
    QTY_EXACT,
    QTY_UNKNOWN,
    UNKNOWN_AWARD,
)
from response_engine.uom import normalize_uom_code

_CLIN_ROW = re.compile(
    r"\b(?:CLIN|Item|Line)\s*[#:]?\s*([0-9A-Z]{1,8})\b"
    r".{0,80}?"
    r"(?:qty|quantity|qnty)?\s*[:#]?\s*(\d[\d,]*(?:\.\d+)?)"
    r"\s*(EA|EACH|CS|CASE|BX|BOX|PK|PACK|KIT|SET|LOT|FT|LF)?",
    re.I | re.S,
)
_SIMPLE_QTY = re.compile(
    r"(?:qty|quantity)\s*[.:]?\s*(\d[\d,]*(?:\.\d+)?)\s*(EA|EACH|CS|CASE|UNIT)?",
    re.I,
)


def new_line_item(
    *,
    response_project_id: str,
    buyer_line_number: str | None = None,
    clin: str | None = None,
    description: str | None = None,
    quantity: str | None = None,
    buyer_uom: str | None = None,
    provenance: dict[str, Any] | None = None,
    product_mode: str | None = None,
    award_basis: str = UNKNOWN_AWARD,
    quantity_state: str = QTY_UNKNOWN,
    **extra: Any,
) -> dict[str, Any]:
    lid = new_id("LI")
    uom = normalize_uom_code(buyer_uom)
    return {
        "kind": "ResponseLineItem",
        "line_item_id": lid,
        "response_project_id": response_project_id,
        "buyer_line_number": buyer_line_number or clin,
        "CLIN": clin,
        "SLIN": extra.get("slin"),
        "lot": extra.get("lot"),
        "group": extra.get("group"),
        "item_number": extra.get("item_number") or buyer_line_number or clin,
        "description": description,
        "quantity": quantity,
        "buyer_uom": uom,
        "normalized_quantity": None,
        "normalized_uom": None,
        "quantity_state": quantity_state if quantity else QTY_UNKNOWN,
        "product_requirement_id": extra.get("product_requirement_id"),
        "NSN": extra.get("nsn"),
        "manufacturer": extra.get("manufacturer"),
        "required_mpn": extra.get("required_mpn"),
        "required_brand": extra.get("required_brand"),
        "product_mode": product_mode,
        "alternate_allowed": extra.get("alternate_allowed"),
        "delivery_location": extra.get("delivery_location"),
        "delivery_date": extra.get("delivery_date"),
        "period": extra.get("period") or "BASE",
        "option_period": extra.get("option_period"),
        "award_basis": award_basis or UNKNOWN_AWARD,
        "unit_price_response_required": True,
        "extended_price_response_required": True,
        "no_bid_allowed": bool(extra.get("no_bid_allowed")),
        "line_status": "OPEN",
        "provenance": provenance or empty_provenance(extractor="r2_clin"),
        "parent_line_item_id": extra.get("parent_line_item_id"),
        "selected_offered_product_id": None,
        "template_map": extra.get("template_map"),
    }


def extract_line_items_from_project(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract CLINS/lines from parsed tables + text. Never invent quantities."""
    rid = project["response_project_id"]
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()

    product_mode = project.get("product_mode")
    award = _infer_award_basis(project)

    for doc in project.get("documents") or []:
        if doc.get("controlling_status") == "SUPERSEDED":
            continue
        doc_id = doc.get("document_id")
        # Structured tables from parsers
        tables = doc.get("tables") or {}
        for li in tables.get("line_items") or []:
            clin = str(li.get("clin") or li.get("item") or li.get("line") or "").strip() or None
            qty = li.get("quantity") or li.get("qty")
            uom = li.get("uom") or li.get("unit")
            desc = li.get("description") or li.get("desc") or li.get("item_description")
            key = f"{clin}|{qty}|{desc}"
            if key in seen:
                continue
            seen.add(key)
            sheet = li.get("sheet") or li.get("sheet_name")
            row = li.get("row")
            prov = empty_provenance(
                document_id=doc_id,
                source_anchor=f"{sheet or 'table'}/row {row}" if row else sheet,
                excerpt=str(desc or clin or "")[:200],
                extractor="r2_table",
                confidence="HIGH" if qty else "MEDIUM",
            )
            if sheet or row is not None:
                prov["sheet"] = sheet
                prov["row"] = row
                prov["cell"] = li.get("unit_price_cell") or li.get("cell")
            lines.append(
                new_line_item(
                    response_project_id=rid,
                    clin=clin,
                    buyer_line_number=clin or str(len(lines) + 1),
                    description=str(desc) if desc else None,
                    quantity=str(qty) if qty not in (None, "") else None,
                    buyer_uom=uom,
                    provenance=prov,
                    product_mode=product_mode,
                    award_basis=award,
                    quantity_state=QTY_EXACT if qty not in (None, "") else QTY_UNKNOWN,
                    nsn=li.get("nsn"),
                    manufacturer=li.get("manufacturer"),
                    required_mpn=li.get("mpn") or li.get("part_number"),
                    required_brand=li.get("brand"),
                    template_map=_template_map_from_li(doc_id, li) if sheet else None,
                )
            )

        # Workbook sample cells → pricing template map hints
        wb = doc.get("workbook") or {}
        for sheet in wb.get("sheets") or []:
            name = sheet.get("name")
            for cell in sheet.get("sample_cells") or []:
                # header detection only — do not invent lines from headers
                pass

        text = doc.get("text") or ""
        for m in _CLIN_ROW.finditer(text):
            clin, qty, uom = m.group(1), m.group(2), m.group(3)
            key = f"text|{clin}|{qty}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                new_line_item(
                    response_project_id=rid,
                    clin=clin,
                    buyer_line_number=clin,
                    quantity=qty.replace(",", ""),
                    buyer_uom=uom or "EA",
                    description=m.group(0)[:160],
                    provenance=empty_provenance(
                        document_id=doc_id,
                        excerpt=m.group(0)[:200],
                        extractor="r2_clin_regex",
                        confidence="MEDIUM",
                    ),
                    product_mode=product_mode,
                    award_basis=award,
                    quantity_state=QTY_EXACT,
                )
            )

    # Fallback: single quantity from requirements if no lines
    if not lines:
        qty = None
        uom = "EA"
        for req in project.get("requirements") or []:
            if req.get("superseded"):
                continue
            if req.get("requirement_category") == "QUANTITY":
                m = re.search(r"(\d[\d,]*(?:\.\d+)?)", req.get("requirement_text") or "")
                if m:
                    qty = m.group(1).replace(",", "")
                    break
        if qty:
            lines.append(
                new_line_item(
                    response_project_id=rid,
                    clin="0001",
                    buyer_line_number="0001",
                    quantity=qty,
                    buyer_uom=uom,
                    description=project.get("title") or "Line 0001",
                    provenance=empty_provenance(
                        extractor="r2_qty_fallback",
                        confidence="LOW",
                        excerpt=f"quantity {qty}",
                    ),
                    product_mode=product_mode,
                    award_basis=award,
                    quantity_state=QTY_EXACT,
                )
            )
        else:
            # Structure incomplete — one placeholder without quantity
            lines.append(
                new_line_item(
                    response_project_id=rid,
                    clin="0001",
                    buyer_line_number="0001",
                    description=project.get("title") or "Unresolved line structure",
                    provenance=empty_provenance(extractor="r2_structure_incomplete", confidence="LOW"),
                    product_mode=product_mode,
                    award_basis=award,
                    quantity_state=QTY_UNKNOWN,
                )
            )

    # Attach NSN/MPN from requirements when single-line
    if len(lines) == 1:
        _enrich_from_requirements(lines[0], project)

    return lines


def _template_map_from_li(doc_id: str | None, li: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "BuyerPricingFieldMap",
        "template_document_id": doc_id,
        "sheet": li.get("sheet") or li.get("sheet_name"),
        "row": li.get("row"),
        "unit_price_cell": li.get("unit_price_cell") or li.get("cell"),
        "extended_price_cell": li.get("extended_price_cell"),
        "field_type": "UNIT_PRICE",
        "format": li.get("format"),
        "buyer_formula_present": bool(li.get("formula")),
        "editable": True,
        "mapping_confidence": "HIGH" if li.get("row") is not None else "MEDIUM",
    }


def _infer_award_basis(project: dict[str, Any]) -> str:
    blob = " ".join(
        (d.get("text") or "")[:2000] for d in (project.get("documents") or []) if d.get("controlling_status") != "SUPERSEDED"
    ).lower()
    if "all or none" in blob or "all-or-none" in blob:
        return "ALL_OR_NONE"
    if "aggregate" in blob and "award" in blob:
        return "AGGREGATE"
    if "line item" in blob or "award by line" in blob:
        return LINE_ITEM
    if "multiple award" in blob:
        return "MULTIPLE_AWARD"
    return UNKNOWN_AWARD


def _enrich_from_requirements(line: dict[str, Any], project: dict[str, Any]) -> None:
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        cat = req.get("requirement_category")
        text = req.get("requirement_text") or ""
        if cat in {"EXACT_BRAND", "PRODUCT_IDENTITY"} and not line.get("required_mpn"):
            m = re.search(r"(?:part|mpn|p/?n)\s*[#:.]?\s*([A-Z0-9][A-Z0-9\-./]{2,})", text, re.I)
            if m:
                line["required_mpn"] = m.group(1)
        if cat == "EXACT_BRAND" and not line.get("required_brand"):
            m = re.search(r"brand[\s-]*name\s+only[:\s]+([A-Za-z0-9\- ]{2,40})", text, re.I)
            if m:
                line["required_brand"] = m.group(1).strip()
        nsn = re.search(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b", text)
        if nsn and not line.get("NSN"):
            line["NSN"] = nsn.group(1)
