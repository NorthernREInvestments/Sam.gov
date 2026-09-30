"""R2 OfferedProduct + selection — never invent product identity."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r2_constants import (
    EXACT_PART_NUMBER,
    UNKNOWN,
    UNKNOWN_CONDITION,
    UNKNOWN_PRODUCT_MODE,
)


def new_offered_product(
    *,
    response_project_id: str,
    line_item_id: str,
    manufacturer: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    mpn: str | None = None,
    sku: str | None = None,
    nsn: str | None = None,
    cage: str | None = None,
    description: str | None = None,
    condition: str = UNKNOWN_CONDITION,
    country_of_origin: str | None = None,
    country_of_manufacture: str | None = None,
    warranty: str | None = None,
    supplier: str | None = None,
    supplier_product_id: str | None = None,
    product_mode: str = UNKNOWN_PRODUCT_MODE,
    exact_match_status: str = UNKNOWN,
    evidence_status: str = UNKNOWN,
    superseding_part: bool = False,
    supersession_evidence_id: str | None = None,
    upc: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "OfferedProduct",
        "offered_product_id": new_id("OP"),
        "response_project_id": response_project_id,
        "line_item_id": line_item_id,
        "manufacturer": manufacturer,
        "brand": brand,
        "model": model,
        "MPN": mpn,
        "SKU": sku,
        "UPC_GTIN": upc,
        "NSN": nsn,
        "CAGE": cage,
        "product_description": description,
        "condition": condition or UNKNOWN_CONDITION,
        "country_of_origin": country_of_origin,
        "country_of_manufacture": country_of_manufacture,
        "warranty": warranty,
        "supplier": supplier,
        "supplier_product_id": supplier_product_id,
        "exact_match_status": exact_match_status,
        "product_mode": product_mode or UNKNOWN_PRODUCT_MODE,
        "evidence_status": evidence_status,
        "superseding_part": superseding_part,
        "supersession_evidence_id": supersession_evidence_id,
        "technically_stale": False,
        "selected": False,
    }


def evaluate_exact_match(*, required_mpn: str | None, offered_mpn: str | None, product_mode: str | None) -> str:
    """Deterministic exact-part check. Similarity never creates EXACT."""
    mode = product_mode or UNKNOWN_PRODUCT_MODE
    if mode not in {EXACT_PART_NUMBER, "BRAND_NAME_ONLY"} and not required_mpn:
        return "NOT_APPLICABLE"
    if not required_mpn:
        return UNKNOWN
    if not offered_mpn:
        return UNKNOWN
    if _norm_pn(required_mpn) == _norm_pn(offered_mpn):
        return "EXACT_MATCH"
    return "MISMATCH"


def _norm_pn(pn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(pn).upper())


def select_offered_product(
    project: dict[str, Any], line_item_id: str, offered_product_id: str, *, owner: bool = True
) -> dict[str, Any]:
    products = project.setdefault("offered_products", [])
    lines = project.setdefault("line_items", [])
    for p in products:
        if p.get("line_item_id") == line_item_id:
            p["selected"] = p.get("offered_product_id") == offered_product_id
    for li in lines:
        if li.get("line_item_id") == line_item_id:
            li["selected_offered_product_id"] = offered_product_id
            li["selection_source"] = "OWNER" if owner else "RECOMMENDED"
    return {"ok": True, "line_item_id": line_item_id, "offered_product_id": offered_product_id}
