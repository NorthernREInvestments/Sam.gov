"""Generic deal/opportunity context helpers — no opportunity-specific hardcoding."""

from __future__ import annotations

from typing import Any


def required_quantity_from_bom(bom: list[dict[str, Any]] | None) -> float | int | None:
    if not bom:
        return None
    for key in ("base_system", "part_number", "quantity", "product_quantity"):
        for item in bom:
            if item.get("component") == key and item.get("quantity") is not None:
                try:
                    return float(item["quantity"])
                except (TypeError, ValueError):
                    continue
    for item in bom:
        q = item.get("quantity")
        if q is not None:
            try:
                return float(q)
            except (TypeError, ValueError):
                pass
    return None


def primary_product_label(bom: list[dict[str, Any]] | None) -> str | None:
    if not bom:
        return None
    for key in ("base_system", "product", "part_number"):
        for item in bom:
            if item.get("component") == key and item.get("value"):
                return str(item["value"])
    return None


def part_number_from_bom(bom: list[dict[str, Any]] | None) -> str | None:
    for item in bom or []:
        if item.get("component") == "part_number" and item.get("value"):
            return str(item["value"])
    return None


def destination_from_checkpoint(checkpoint: dict[str, Any] | None) -> str | None:
    if not checkpoint:
        return None
    for p in checkpoint.get("pursuit_plans") or []:
        if isinstance(p, dict) and p.get("destination"):
            return str(p["destination"])
    s3 = checkpoint.get("stage3") or {}
    delivery = s3.get("delivery") if isinstance(s3.get("delivery"), dict) else {}
    if delivery.get("destination"):
        return str(delivery["destination"])
    return checkpoint.get("delivery_destination")


def delivery_requirement_from_deal(
    contract: Any = None,
    checkpoint: dict[str, Any] | None = None,
) -> str:
    for p in (checkpoint or {}).get("pursuit_plans") or []:
        if isinstance(p, dict) and p.get("delivery_deadline"):
            return str(p["delivery_deadline"])
    if contract is not None:
        for r in getattr(contract, "requirement_register", []) or []:
            pass  # populated via workspace
    return "per solicitation delivery requirements"


def oem_letter_required_from_requirements(requirements: list[dict[str, Any]] | None) -> bool | None:
    if not requirements:
        return None
    for r in requirements:
        rt = str(r.get("requirement_type") or r.get("type") or "").upper()
        if rt in {"OEM_LETTER", "OEM_AUTHORIZATION", "CHANNEL_AUTHORIZATION"} and r.get("required"):
            return True
    return None


def format_deadline_close_label(contract: Any) -> str | None:
    if contract is None or not getattr(contract, "due_date", None):
        return None
    d = contract.due_date
    return f"{d.isoformat()} 5:00PM local (operator verify timezone)"
