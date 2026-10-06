"""Per-line acquisition cost recovery with best-price + channel path.

Build: 20261004-m3-acquisition-scale-v1
"""

from __future__ import annotations

from typing import Any

from acquisition_scale.channel_path import identify_channel_quote_path
from acquisition_scale.models import (
    DISTRIBUTOR_QUOTE_REQUIRED,
    OEM_QUOTE_REQUIRED,
    PUBLIC_PRICE_AVAILABLE,
    PUBLIC_PRICE_BLOCKED_RETRYABLE,
    SUPPLIER_QUOTE_REQUIRED,
)
from evidence_exhaustion.exhaust import exhaust_line
from evidence_exhaustion.routing import classify_product_routing
from public_price_search.models import (
    CONDITION_MISMATCH,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PRICE_SOURCE_BLOCKED_RETRYABLE,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
)


_DESC_MFR = (
    (r"\bF\.?\s*E\.?\s*MYERS\b", "FE Myers"),
    (r"\bMYERS\b", "Myers"),
    (r"\bCUMMINS\b", "Cummins"),
    (r"\bFLEETGUARD\b", "Fleetguard"),
    (r"\bCATERPILLAR\b|\bCAT\b", "Caterpillar"),
    (r"\bGRAINGER\b", "Grainger"),
    (r"\bDYNAREX\b", "Dynarex"),
    (r"\bSMITH[- ]BLAIR\b", "Smith-Blair"),
    (r"\bFORD\b", "Ford"),
    (r"\bPENTAIR\b", "Pentair"),
    (r"\bGOULDS\b", "Goulds"),
)


def _enrich_identity_for_cost(identity: dict[str, Any]) -> dict[str, Any]:
    """Fill manufacturer from description when missing — improves OEM/distributor routing."""
    import re

    out = dict(identity)
    if out.get("manufacturer") or out.get("brand"):
        return out
    blob = " ".join(
        str(out.get(k) or "")
        for k in ("raw_description", "description", "item_name", "title")
    )
    if not blob:
        return out
    for pat, name in _DESC_MFR:
        if re.search(pat, blob, re.I):
            out["manufacturer"] = name
            out["brand"] = out.get("brand") or name
            break
    return out


def recover_line_cost(
    identity: dict[str, Any],
    *,
    opportunity_id: str,
    channel: dict[str, Any] | None = None,
    use_budget: bool = True,
    max_queries: int = 4,
    max_pages: int = 6,
) -> dict[str, Any]:
    """Fight for current NEW public cost; identify quote path if needed. No outreach."""
    identity = _enrich_identity_for_cost(identity)
    routing = classify_product_routing(identity)
    out = exhaust_line(
        identity,
        opportunity_id=opportunity_id,
        eligibility_ok=True,
        has_package=True,
        use_budget=use_budget,
        max_queries=max_queries,
        max_pages=max_pages,
    )
    ev = out.get("evidence")
    status = out.get("status")
    priced = bool(ev and ev.get("unit_price"))
    blocked = status in {
        PRICE_SOURCE_BLOCKED_RETRYABLE,
        "ROUTE_BLOCKED_RETRYABLE",
        "RESEARCH_RETRYABLE",
    }
    # Prefer channel from revenue store if provided
    ch = channel or out.get("channel") or {}
    path = identify_channel_quote_path(
        identity,
        channel=ch,
        public_price_found=priced,
        public_price_blocked=blocked or status in {
            NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
            CONDITION_MISMATCH,
            "QUOTE_OUTREACH_RESERVE",
        },
    )

    via = str((ev or {}).get("via") or "")
    seller_class = str((ev or {}).get("seller_class") or "")
    source_bucket = "other"
    if "structured" in via:
        source_bucket = "structured"
    elif seller_class == "MANUFACTURER" or "manufacturer" in via:
        source_bucket = "manufacturer"
    elif "authorized" in via:
        source_bucket = "authorized_distributor"
    elif seller_class == "DISTRIBUTOR" or "distributor" in via:
        source_bucket = "distributor"
    elif seller_class == "RESELLER" or "serp" in via:
        source_bucket = "reseller"
    elif "catalog" in via or "pdf" in via:
        source_bucket = "catalog_pdf"
    elif via:
        source_bucket = "direct_catalog"

    if routing["routing_class"] == "BRAND_OR_EQUAL" and priced:
        source_bucket = "brand_equal"
    if routing["routing_class"] == "STRONG_GENERIC_SPEC" and priced:
        source_bucket = "generic_spec"

    quote_hidden = None
    premature = False
    if not priced and path.get("quote_only_status"):
        # Only admit quote-needed after exhaustion flags say so
        flags = out.get("exhaustion_flags") or {}
        exhausted = all(
            flags.get(k)
            for k in (
                "manufacturer_search_exhausted",
                "distributor_search_exhausted",
                "direct_catalog_exhausted",
                "alternate_domain_search_exhausted",
            )
        )
        if exhausted and int(out.get("exhaustion_score") or 0) >= 60:
            quote_hidden = path.get("quote_substatus") or SUPPLIER_QUOTE_REQUIRED
        else:
            premature = False  # correctly not admitted
            quote_hidden = None

    # Keep blocked/retryable surface status — quote path is additive metadata only
    if priced:
        final_status = PUBLIC_PRICE_AVAILABLE
    elif blocked:
        final_status = "ROUTE_BLOCKED_RETRYABLE"
    elif quote_hidden:
        final_status = path.get("status") or status
    else:
        final_status = status

    return {
        "opportunity_id": opportunity_id,
        "identity": {
            "part_number": identity.get("part_number"),
            "manufacturer": identity.get("manufacturer") or identity.get("brand"),
            "model": identity.get("model"),
            "routing": routing["routing_class"],
            "description": (identity.get("raw_description") or "")[:160],
        },
        "status": final_status,
        "unit_price": (ev or {}).get("unit_price"),
        "condition": (ev or {}).get("condition"),
        "seller": (ev or {}).get("seller"),
        "source_url": (ev or {}).get("source_url"),
        "source_bucket": source_bucket if priced else None,
        "selection_reason": (ev or {}).get("selection_reason"),
        "n_candidates": out.get("n_new_candidates") or len(out.get("candidates") or []),
        "best_price_improvement": bool(out.get("best_price_improvement")),
        "recovered_via_alternate": bool(out.get("recovered_via_alternate")),
        "primary_status": out.get("primary_status") or status,
        "exhaustion_score": out.get("exhaustion_score"),
        "channel_path": path,
        "quote_hidden_substatus": quote_hidden,
        "premature_quote_admission": premature,
        "used_history_as_cost": False,
        "candidates": [
            {
                "price": c.get("unit_price"),
                "condition": c.get("condition"),
                "url": c.get("url"),
                "via": c.get("via"),
            }
            for c in (out.get("candidates") or [])[:6]
        ],
    }
