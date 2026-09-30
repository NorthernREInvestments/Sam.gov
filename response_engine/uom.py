"""R2 UOM normalization — fail-closed conversion (no assumed pack sizes)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from response_engine.money import D, as_str
from response_engine.r2_constants import UOM_CONVERSION_BLOCKED

# Canonical aliases → normalized code
UOM_ALIASES: dict[str, str] = {
    "EA": "EA",
    "EACH": "EA",
    "UNIT": "EA",
    "PC": "EA",
    "PCS": "EA",
    "PIECE": "EA",
    "CS": "CASE",
    "CASE": "CASE",
    "BX": "BOX",
    "BOX": "BOX",
    "PK": "PACK",
    "PACK": "PACK",
    "PKG": "PACK",
    "DOZEN": "DOZEN",
    "DZ": "DOZEN",
    "DOZ": "DOZEN",
    "SET": "SET",
    "KIT": "KIT",
    "KT": "KIT",
    "LOT": "LOT",
    "PAIR": "PAIR",
    "PR": "PAIR",
    "FT": "FT",
    "LF": "FT",
    "FOOT": "FT",
    "FEET": "FT",
    "YD": "YD",
    "YARD": "YD",
    "GAL": "GAL",
    "GALLON": "GAL",
    "LB": "LB",
    "LBS": "LB",
    "POUND": "LB",
    "KG": "KG",
    "MONTH": "MONTH",
    "MO": "MONTH",
    "YEAR": "YEAR",
    "YR": "YEAR",
}

PACK_UOMS = frozenset({"CASE", "BOX", "PACK"})
FIXED_FACTORS: dict[str, Decimal] = {
    "DOZEN": Decimal("12"),
    "PAIR": Decimal("2"),
}


def normalize_uom_code(raw: Any) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    key = str(raw).strip().upper().replace(".", "")
    return UOM_ALIASES.get(key, key)


def convert_quantity(
    *,
    buyer_qty: Any,
    buyer_uom: Any,
    supplier_qty: Any = None,
    supplier_uom: Any = None,
    pack_size: Any = None,
    roll_length: Any = None,
) -> dict[str, Any]:
    """Normalize buyer quantity to EA when possible. Never invent pack size."""
    bq = D(buyer_qty)
    bu = normalize_uom_code(buyer_uom) or "EA"
    su = normalize_uom_code(supplier_uom) if supplier_uom is not None else None
    sq = D(supplier_qty)
    pack = D(pack_size)
    roll = D(roll_length)

    if bq is None:
        return {
            "ok": False,
            "status": "QUANTITY_UNKNOWN",
            "normalized_quantity": None,
            "normalized_uom": None,
            "block": "QUANTITY_UNKNOWN",
        }

    # Buyer already in EA family
    if bu == "EA":
        return {
            "ok": True,
            "status": "NORMALIZED",
            "normalized_quantity": as_str(bq),
            "normalized_uom": "EA",
            "buyer_qty": as_str(bq),
            "buyer_uom": bu,
            "block": None,
        }

    if bu in FIXED_FACTORS:
        return {
            "ok": True,
            "status": "NORMALIZED",
            "normalized_quantity": as_str(bq * FIXED_FACTORS[bu]),
            "normalized_uom": "EA",
            "buyer_qty": as_str(bq),
            "buyer_uom": bu,
            "factor": as_str(FIXED_FACTORS[bu]),
            "block": None,
        }

    if bu in PACK_UOMS:
        if pack is None or pack <= 0:
            return {
                "ok": False,
                "status": UOM_CONVERSION_BLOCKED,
                "normalized_quantity": None,
                "normalized_uom": "EA",
                "buyer_qty": as_str(bq),
                "buyer_uom": bu,
                "block": UOM_CONVERSION_BLOCKED,
                "reason": "pack size unknown",
            }
        return {
            "ok": True,
            "status": "NORMALIZED",
            "normalized_quantity": as_str(bq * pack),
            "normalized_uom": "EA",
            "buyer_qty": as_str(bq),
            "buyer_uom": bu,
            "pack_size": as_str(pack),
            "block": None,
        }

    # Supplier CASE vs buyer EA
    if bu == "EA" and su in PACK_UOMS:
        if pack is None or pack <= 0:
            return {
                "ok": False,
                "status": UOM_CONVERSION_BLOCKED,
                "normalized_quantity": as_str(bq),
                "normalized_uom": "EA",
                "block": UOM_CONVERSION_BLOCKED,
                "reason": "supplier CASE/BOX pack size unknown",
            }

    if bu == "FT" and su in {"ROLL", "RL"} and (roll is None or roll <= 0):
        return {
            "ok": False,
            "status": UOM_CONVERSION_BLOCKED,
            "normalized_quantity": None,
            "normalized_uom": "FT",
            "block": UOM_CONVERSION_BLOCKED,
            "reason": "roll length unknown",
        }

    # Same UOM — pass through
    if su is None or su == bu:
        return {
            "ok": True,
            "status": "PASSTHROUGH",
            "normalized_quantity": as_str(bq),
            "normalized_uom": bu,
            "buyer_qty": as_str(bq),
            "buyer_uom": bu,
            "block": None,
        }

    return {
        "ok": False,
        "status": UOM_CONVERSION_BLOCKED,
        "normalized_quantity": None,
        "normalized_uom": bu,
        "buyer_qty": as_str(bq),
        "buyer_uom": bu,
        "supplier_uom": su,
        "block": UOM_CONVERSION_BLOCKED,
        "reason": f"cannot convert {su} → {bu} without factor",
    }
