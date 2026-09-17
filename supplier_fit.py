"""Supplier/product fit against extracted specifications — no outreach."""

from __future__ import annotations

import re
from typing import Any

from document_ingestion_constants import (
    FIT_EXACT,
    FIT_INSUFFICIENT,
    FIT_LIKELY,
    FIT_NOT,
    FIT_POSSIBLE,
    FIT_QUOTE,
)


def evaluate_supplier_fit(
    *,
    suppliers: list[dict[str, Any]],
    specifications: dict[str, Any] | None,
    product_id: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Score curated supplier candidates against extracted specs.
    Does not invent authorization, SKUs, or prices.
    """
    specs = (specifications or {}).get("specifications") or specifications or {}
    product_id = product_id or {}
    sourcing = (product_id.get("sourcing_description") or "").lower()
    carbide = str((specs.get("carbide_configuration") or {}).get("value") or "").lower()
    brand_equal = bool((specs.get("brand_or_equal_language") or {}).get("value"))

    out: list[dict[str, Any]] = []
    for s in suppliers or []:
        name = (s.get("supplier_name") or "").lower()
        stype = (s.get("supplier_type") or "").upper()
        notes = (s.get("notes") or "").lower()
        public_sku = s.get("part_number") or s.get("model")
        fit = FIT_INSUFFICIENT
        reason = "insufficient_spec_or_product_evidence"

        if not specs and not sourcing:
            fit = FIT_INSUFFICIENT
        elif public_sku and carbide and public_sku.lower() in sourcing:
            fit = FIT_EXACT
            reason = "public_sku_aligned_with_requirement"
        elif stype == "MANUFACTURER" and ("carbide" in notes or "blade" in notes or "kennametal" in name or "valley" in name):
            fit = FIT_LIKELY if (carbide or "carbide" in sourcing or "blade" in sourcing) else FIT_POSSIBLE
            reason = "manufacturer_in_relevant_category"
        elif "winter equipment" in name or "snow" in notes or "plow" in notes:
            fit = FIT_LIKELY if ("blade" in sourcing or carbide) else FIT_POSSIBLE
            reason = "specialty_snowplow_supplier"
        elif "grainger" in name or "msc" in name or stype == "ESTABLISHED_DISTRIBUTOR":
            fit = FIT_POSSIBLE
            reason = "industrial_distributor_path_unverified_for_exact_spec"
        elif re.search(r"consumer|amazon|ebay", name):
            fit = FIT_NOT
            reason = "consumer_marketplace_not_preferred"
        else:
            fit = FIT_POSSIBLE
            reason = "unverified_category_overlap"

        # Without public price / confirmed SKU compliance → quote required overlay
        price_basis = s.get("price_basis") or s.get("public_price")
        quote_required = True
        if s.get("public_price") is not None and fit == FIT_EXACT:
            quote_required = False
        if fit != FIT_NOT:
            # Operational state for quoting
            fit_state = FIT_QUOTE if quote_required and fit != FIT_EXACT else fit
            if quote_required and fit in {FIT_LIKELY, FIT_POSSIBLE, FIT_EXACT}:
                fit_state = FIT_QUOTE if fit != FIT_EXACT else fit
        else:
            fit_state = FIT_NOT

        # Prefer showing both category fit and quote need
        out.append(
            {
                **s,
                "fit_category": fit,
                "fit_state": FIT_QUOTE if quote_required and fit not in {FIT_NOT, FIT_INSUFFICIENT} else fit_state,
                "fit_reason": reason,
                "public_sku_or_part": public_sku,
                "quote_required": quote_required,
                "brand_or_equal_context": brand_equal,
                "authorized_distributor_evidence": s.get("authorized_distributor_evidence"),  # never invent
                "outreach_performed": False,
            }
        )
    return out
