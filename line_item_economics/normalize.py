"""Pack / UOM normalization — never compare mismatched units."""

from __future__ import annotations

from typing import Any

from response_engine.uom import convert_quantity, normalize_uom_code


def normalize_line_quantity(line: dict[str, Any]) -> dict[str, Any]:
    """Normalize buyer qty to EA when pack size known. Fail closed otherwise."""
    qty = line.get("quantity")
    uom = line.get("unit_of_measure")
    pack = line.get("pack_size")
    result = convert_quantity(
        buyer_qty=qty,
        buyer_uom=uom or "EA",
        pack_size=pack,
    )
    line["normalization"] = result
    if result.get("ok") and result.get("normalized_quantity") is not None:
        try:
            line["normalized_quantity"] = float(result["normalized_quantity"])
        except (TypeError, ValueError):
            line["normalized_quantity"] = None
        line["normalized_uom"] = result.get("normalized_uom") or "EA"
        line["unit_mismatch"] = False
    else:
        line["normalized_quantity"] = None
        line["normalized_uom"] = normalize_uom_code(uom) or uom
        line["unit_mismatch"] = bool(result.get("block"))
        if qty is not None and (normalize_uom_code(uom) or "EA") == "EA":
            try:
                line["normalized_quantity"] = float(qty)
                line["normalized_uom"] = "EA"
                line["unit_mismatch"] = False
            except (TypeError, ValueError):
                pass
    return line


def normalize_retail_to_buyer_units(
    *,
    retail_unit_price: float | None,
    retail_uom: str | None,
    retail_pack_size: float | None,
    buyer_uom: str | None,
    buyer_pack_size: float | None,
) -> dict[str, Any]:
    """Convert retail price into buyer UOM unit price. Refuse mismatched units."""
    if retail_unit_price is None:
        return {"ok": False, "normalized_unit_price": None, "reason": "NO_RETAIL_PRICE"}
    bu = normalize_uom_code(buyer_uom) or "EA"
    ru = normalize_uom_code(retail_uom) or "EA"

    # Retail priced per each, buyer wants cases
    if ru == "EA" and bu in {"CASE", "BOX", "PACK"}:
        pack = buyer_pack_size
        if pack is None or pack <= 0:
            return {"ok": False, "normalized_unit_price": None, "reason": "PACK_SIZE_UNKNOWN"}
        return {
            "ok": True,
            "normalized_unit_price": float(retail_unit_price) * float(pack),
            "normalized_uom": bu,
            "conversion": f"EA*{pack}->{bu}",
        }

    # Retail case price, buyer each
    if ru in {"CASE", "BOX", "PACK"} and bu == "EA":
        pack = retail_pack_size or buyer_pack_size
        if pack is None or pack <= 0:
            return {"ok": False, "normalized_unit_price": None, "reason": "PACK_SIZE_UNKNOWN"}
        return {
            "ok": True,
            "normalized_unit_price": float(retail_unit_price) / float(pack),
            "normalized_uom": "EA",
            "conversion": f"{ru}/{pack}->EA",
        }

    # Dozen
    if ru == "DOZEN" and bu == "EA":
        return {"ok": True, "normalized_unit_price": float(retail_unit_price) / 12.0, "normalized_uom": "EA"}
    if ru == "EA" and bu == "DOZEN":
        return {"ok": True, "normalized_unit_price": float(retail_unit_price) * 12.0, "normalized_uom": "DOZEN"}

    if ru == bu:
        return {"ok": True, "normalized_unit_price": float(retail_unit_price), "normalized_uom": bu}

    return {
        "ok": False,
        "normalized_unit_price": None,
        "reason": "UNIT_MISMATCH",
        "retail_uom": ru,
        "buyer_uom": bu,
    }


def normalize_all(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for line in lines:
        normalize_line_quantity(line)
    return lines
