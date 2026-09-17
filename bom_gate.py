"""BOM completeness gate for product resale."""

from __future__ import annotations

from typing import Any

BOM_COMPLETE = "BOM_COMPLETE"
BOM_INCOMPLETE = "BOM_INCOMPLETE"
BOM_UNRESOLVED = "BOM_UNRESOLVED"


def evaluate_bom_completeness(
    bom_items: list[dict[str, Any]] | None,
    *,
    supplier_configurable_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """
    BOM_COMPLETE only when every solicitation-required config is resolved
    OR explicitly documented as supplier-configurable from the solicitation.
    """
    items = [i for i in (bom_items or []) if isinstance(i, dict)]
    if not items:
        return {
            "status": BOM_UNRESOLVED,
            "blockers": ["bom_empty"],
            "unknown_components": [],
            "supplier_quote_request_ready": False,
        }

    configurable = set(supplier_configurable_keys or ())
    unknown: list[str] = []
    for item in items:
        key = str(item.get("component") or item.get("key") or "")
        st = str(item.get("status") or "UNKNOWN").upper()
        qty = item.get("quantity")
        # Explicit supplier-configurable waiver
        if key in configurable or item.get("supplier_configurable") is True:
            continue
        if st in {"UNKNOWN", "REQUIRED_UNKNOWN"} or (
            key
            and ("quantity" in key.lower() or "qty" in key.lower() or "module" in key.lower())
            and qty is None
            and st != "VERIFIED"
        ):
            # Memory/storage unknowns that are part of required config
            if st != "NOT_APPLICABLE":
                unknown.append(key or str(item.get("value") or "unnamed"))
        # Component marked UNKNOWN without configurable flag
        if st == "UNKNOWN" and key not in configurable:
            if key not in unknown:
                unknown.append(key)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unknown_u = []
    for u in unknown:
        if u not in seen:
            seen.add(u)
            unknown_u.append(u)

    if unknown_u:
        return {
            "status": BOM_INCOMPLETE,
            "blockers": [f"unresolved_bom:{u}" for u in unknown_u],
            "unknown_components": unknown_u,
            "supplier_quote_request_ready": False,
        }

    # All items resolved
    unresolved_meta = any(
        str(i.get("status") or "").upper() in {"", "UNRESOLVED"} for i in items
    )
    if unresolved_meta:
        return {
            "status": BOM_UNRESOLVED,
            "blockers": ["bom_item_status_unresolved"],
            "unknown_components": [],
            "supplier_quote_request_ready": False,
        }

    return {
        "status": BOM_COMPLETE,
        "blockers": [],
        "unknown_components": [],
        "supplier_quote_request_ready": True,
    }
