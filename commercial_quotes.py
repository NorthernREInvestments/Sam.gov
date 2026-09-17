"""Quote entry, detailed validation mismatches, comparison, document foundation."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from quote_validation import (
    QUOTE_EXPIRED,
    QUOTE_INCOMPLETE,
    QUOTE_MISMATCH,
    QUOTE_UNVERIFIED,
    QUOTE_VALID,
    validate_supplier_quote,
)

QUOTE_DOCUMENT_RECEIVED = "QUOTE_DOCUMENT_RECEIVED"
EXTRACTION_PENDING = "EXTRACTION_PENDING"
OPERATOR_REVIEW_REQUIRED = "OPERATOR_REVIEW_REQUIRED"
OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
VERIFIED_FROM_DOCUMENT = "VERIFIED_FROM_DOCUMENT"

_VERIFIED_STATUSES = frozenset(
    {"VERIFIED", "OPERATOR_CONFIRMED", "VERIFIED_FROM_DOCUMENT"}
)


def _dec(val: Any) -> Decimal | None:
    if val is None or val == "" or val == "UNKNOWN":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _is_unknown(val: Any) -> bool:
    return val is None or val == "" or val == "UNKNOWN"


def normalize_quote_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Operator quote entry — blank is NOT zero.
    Auto-calculate extended = unit * qty when both present and extended blank.
    Never invent freight=0 or total with unknown freight.
    """
    qty = _dec(raw.get("quantity"))
    unit = _dec(raw.get("unit_price"))
    extended = _dec(raw.get("extended_product_price") or raw.get("extended_price"))
    freight_raw = raw.get("freight")
    other_raw = raw.get("other_required_charges")
    total_raw = raw.get("total")

    freight = None if _is_unknown(freight_raw) else _dec(freight_raw)
    other = None if _is_unknown(other_raw) else _dec(other_raw)
    total = None if _is_unknown(total_raw) else _dec(total_raw)

    calc_notes: list[str] = []
    if extended is None and unit is not None and qty is not None:
        extended = unit * qty
        calc_notes.append("extended_product_price=unit_price*quantity")

    freight_known = not _is_unknown(freight_raw)
    other_known = not _is_unknown(other_raw)
    if total is None and extended is not None and freight_known and other_known:
        total = extended + (freight or Decimal("0")) + (other or Decimal("0"))
        calc_notes.append("total=extended+freight+other")
    # If freight unknown: do NOT set total from extended alone as acquisition total

    def _field_status(val: Any, *, calculated: bool = False) -> str:
        if calculated:
            return "CALCULATED"
        if _is_unknown(val):
            return "UNKNOWN"
        return "OPERATOR_ENTERED"

    return {
        "supplier_id": raw.get("supplier_id"),
        "supplier_name": raw.get("supplier_name"),
        "quote_number": raw.get("quote_number"),
        "quote_date": raw.get("quote_date"),
        "expiration_date": raw.get("expires") or raw.get("expiration_date"),
        "quantity": float(qty) if qty is not None else None,
        "quantity_status": _field_status(raw.get("quantity")),
        "unit_price": float(unit) if unit is not None else None,
        "unit_price_status": _field_status(raw.get("unit_price")),
        "extended_price": float(extended) if extended is not None else None,
        "extended_price_status": (
            "CALCULATED"
            if "extended_product_price=unit_price*quantity" in calc_notes
            else _field_status(raw.get("extended_product_price") or raw.get("extended_price"))
        ),
        "freight": float(freight) if freight is not None else None,
        "freight_status": "UNKNOWN" if not freight_known else "OPERATOR_ENTERED",
        "other_required_charges": float(other) if other is not None else None,
        "other_charges_status": "UNKNOWN" if not other_known else "OPERATOR_ENTERED",
        "total": float(total) if total is not None else None,
        "total_status": (
            "CALCULATED"
            if total is not None and "total=extended+freight+other" in calc_notes
            else _field_status(total_raw)
        ),
        "payment_terms": raw.get("payment_terms"),
        "availability": raw.get("availability"),
        "lead_time": raw.get("lead_time"),
        "delivery_confirmed": raw.get("delivery_confirmed"),
        "oem_letter_available": raw.get("oem_letter_available"),
        "federal_channel_confirmed": raw.get("federal_channel_confirmed"),
        "upfront_payment_required": raw.get("upfront_payment_required"),
        "document_notes": raw.get("document_notes") or raw.get("notes"),
        "bom_match": raw.get("bom_match"),
        "exact_bom": raw.get("exact_bom"),
        "memory_modules_per_server_quoted": raw.get("memory_modules_per_server_quoted"),
        "storage_drives_per_server_quoted": raw.get("storage_drives_per_server_quoted"),
        "verification_status": raw.get("verification_status") or "OPERATOR_ENTERED",
        "calc_notes": calc_notes,
        "blank_is_not_zero": True,
        "provenance": "OPERATOR_ENTERED",
    }


def validate_quote_against_bom(
    quote: dict[str, Any],
    *,
    required_bom: list[dict[str, Any]] | None,
    required_quantity: int | float = 14,
    required_memory_per_server: int | None = 8,
    required_storage_per_server: int | None = 6,
    today: date | None = None,
) -> dict[str, Any]:
    """Detailed mismatch messages; blank freight does not become zero."""
    mismatches: list[str] = []
    verification = str(quote.get("verification_status") or "").upper()
    mapped_verification = "VERIFIED" if verification in _VERIFIED_STATUSES else verification

    base = validate_supplier_quote(
        {
            **quote,
            "verification_status": mapped_verification or "UNKNOWN",
            "quantity": quote.get("quantity"),
            "unit_price": quote.get("unit_price"),
            "extended_price": quote.get("extended_price"),
            "expiration_date": quote.get("expiration_date"),
            "bom_match": quote.get("bom_match"),
            "exact_bom": quote.get("exact_bom") or quote.get("bom"),
        },
        required_bom=required_bom,
        required_quantity=required_quantity,
        today=today,
    )

    mem_q = quote.get("memory_modules_per_server_quoted")
    if required_memory_per_server is not None and mem_q is not None:
        try:
            if int(mem_q) != int(required_memory_per_server):
                mismatches.append(
                    f"Memory: required {required_memory_per_server} × 16GB/server; "
                    f"quoted {mem_q} × 16GB/server"
                )
        except (TypeError, ValueError):
            mismatches.append("Memory: quoted quantity unparseable")

    stor_q = quote.get("storage_drives_per_server_quoted")
    if required_storage_per_server is not None and stor_q is not None:
        try:
            if int(stor_q) != int(required_storage_per_server):
                mismatches.append(
                    f"Storage: required {required_storage_per_server} drives/server; "
                    f"quoted {stor_q} drives/server"
                )
        except (TypeError, ValueError):
            mismatches.append("Storage: quoted quantity unparseable")

    qty = quote.get("quantity")
    if qty is not None:
        try:
            if float(qty) != float(required_quantity):
                mismatches.append(f"Quantity: required {required_quantity}; quoted {qty}")
        except (TypeError, ValueError):
            pass

    if mismatches:
        return {
            "status": QUOTE_MISMATCH,
            "usable_as_acquisition_cost": False,
            "blockers": list(base.get("blockers") or []) + ["configuration_mismatch"],
            "mismatches": mismatches,
            "display": "QUOTE MISMATCH\n" + "\n".join(mismatches),
        }

    out = dict(base)
    out["mismatches"] = []
    if out.get("status") == QUOTE_VALID and quote.get("freight_status") == "UNKNOWN":
        out["status"] = QUOTE_INCOMPLETE
        out["usable_as_acquisition_cost"] = False
        out["blockers"] = list(out.get("blockers") or []) + ["freight_unknown"]
        out["notes"] = "Product pricing present but freight UNKNOWN — blank is not zero"
    if out.get("status") == QUOTE_EXPIRED:
        out["mismatches"] = []
    return out


def compare_supplier_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare only valid/usable quotes. Incomplete quotes are never 'cheapest'."""
    rows = []
    for q in quotes or []:
        v = q.get("validation") or {}
        status = v.get("status") or q.get("validation_status")
        usable = bool(v.get("usable_as_acquisition_cost")) and status == QUOTE_VALID
        total = q.get("total")
        if (
            total is None
            and q.get("extended_price") is not None
            and q.get("freight") is not None
            and q.get("freight_status") != "UNKNOWN"
        ):
            try:
                total = (
                    float(q["extended_price"])
                    + float(q["freight"])
                    + float(q.get("other_required_charges") or 0)
                )
            except (TypeError, ValueError):
                total = None
        rows.append(
            {
                "supplier": q.get("supplier_name") or q.get("supplier_id"),
                "validation_status": status,
                "usable": usable,
                "unit_price": q.get("unit_price"),
                "extended_price": q.get("extended_price"),
                "freight": q.get("freight"),
                "freight_status": q.get("freight_status"),
                "total_acquisition_cost": total if usable else None,
                "availability": q.get("availability"),
                "lead_time": q.get("lead_time"),
                "payment_terms": q.get("payment_terms"),
                "channel": q.get("federal_channel_confirmed"),
                "oem_letter": q.get("oem_letter_available"),
                "upfront_cash": q.get("upfront_payment_required"),
            }
        )

    usable_rows = [r for r in rows if r["usable"] and r["total_acquisition_cost"] is not None]
    lowest = None
    if usable_rows:
        lowest = min(usable_rows, key=lambda r: float(r["total_acquisition_cost"]))

    return {
        "rows": rows,
        "lowest_verified_compliant_acquisition_cost": lowest,
        "notes": "Incomplete quotes are never labeled cheapest",
        "LIVE_API_REQUESTS": 0,
    }


def quote_document_record(
    *,
    filename: str,
    content_hash: str | None,
    supplier_id: int | None,
    opportunity_id: int | None,
    quote_number: str | None = None,
    received_date: str | None = None,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "hash": content_hash,
        "supplier_id": supplier_id,
        "opportunity_id": opportunity_id,
        "quote_number": quote_number,
        "received_date": received_date or today_local().isoformat(),
        "verification_state": QUOTE_DOCUMENT_RECEIVED,
        "extraction_status": EXTRACTION_PENDING,
        "notes": "Future AI extraction requires operator review before VERIFIED_FROM_DOCUMENT",
        "auto_verify": False,
    }


def bom_memory_per_server(bom: list[dict[str, Any]] | None) -> int | None:
    for item in bom or []:
        if item.get("component") == "memory_module_quantity_per_server":
            try:
                return int(item.get("value") or item.get("quantity"))
            except (TypeError, ValueError):
                pass
        if item.get("component") == "memory_module" and item.get("quantity_per_server") is not None:
            try:
                return int(item["quantity_per_server"])
            except (TypeError, ValueError):
                pass
    return None


def bom_storage_per_server(bom: list[dict[str, Any]] | None) -> int | None:
    for item in bom or []:
        if item.get("component") == "storage_drive" and item.get("quantity_per_server") is not None:
            try:
                return int(item["quantity_per_server"])
            except (TypeError, ValueError):
                pass
    return None
