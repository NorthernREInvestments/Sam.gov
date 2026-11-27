"""Transparent commercial evidence views — never fabricate verified net profit."""

from __future__ import annotations

from typing import Any


def build_commercial_evidence(
    *,
    quantity: float | None = None,
    historical_unit_price: float | None = None,
    historical_match_type: str | None = None,
    historical_match_reason: str | None = None,
    historical_relevance: str | None = None,
    catalog_unit_price: float | None = None,
    catalog_supplier: str | None = None,
    catalog_effective_date: str | None = None,
    confirmed_quote_unit_price: float | None = None,
    proposed_sell_unit_price: float | None = None,
) -> dict[str, Any]:
    qty = float(quantity) if quantity not in (None, "") else None
    hist = float(historical_unit_price) if historical_unit_price is not None else None
    cat = float(catalog_unit_price) if catalog_unit_price is not None else None
    quote = float(confirmed_quote_unit_price) if confirmed_quote_unit_price is not None else None
    sell = float(proposed_sell_unit_price) if proposed_sell_unit_price is not None else None

    # Weak / unusable historical matches must not drive revenue benchmarks
    relevance = (historical_relevance or "").strip().upper()
    if relevance in {"UNUSABLE", "MARKET_CONTEXT_ONLY", "GENERAL_MARKET_CONTEXT_ONLY"}:
        hist = None

    hist_rev = (hist * qty) if hist is not None and qty is not None else None
    cat_acq = (cat * qty) if cat is not None and qty is not None else None
    quote_acq = (quote * qty) if quote is not None and qty is not None else None
    proposed_rev = (sell * qty) if sell is not None and qty is not None else None

    acquisition = quote_acq if quote_acq is not None else cat_acq
    revenue_bench = proposed_rev if proposed_rev is not None else hist_rev
    gross_spread = None
    if revenue_bench is not None and acquisition is not None:
        gross_spread = revenue_bench - acquisition

    return {
        "quantity": qty,
        "historical_unit_price": hist,
        "historical_match_type": historical_match_type,
        "historical_match_reason": historical_match_reason,
        "historical_relevance": historical_relevance,
        "historical_revenue_benchmark": hist_rev,
        "catalog_unit_price": cat,
        "catalog_supplier": catalog_supplier,
        "catalog_effective_date": catalog_effective_date,
        "catalog_acquisition_benchmark": cat_acq,
        "confirmed_quote_unit_price": quote,
        "confirmed_acquisition_quote": quote_acq,
        "proposed_selling_price_total": proposed_rev,
        "gross_spread": gross_spread,
        "gross_spread_is_not_verified_net_profit": True,
        "preliminary_projected_net_profit": None,  # requires execution costs
        "verified_projected_net_profit": None,
        "profitability_status": (
            "UNKNOWN"
            if revenue_bench is None or acquisition is None
            else "GROSS_SPREAD_ONLY_COSTS_INCOMPLETE"
        ),
        "notes": [
            "Historical award prices are competitive benchmarks, not guaranteed future selling prices.",
            "Catalog prices may be stale and are not confirmed quotes.",
            "Do not treat gross spread as verified net profit.",
            "Missing freight, financing, fees, or taxes keeps final net profit UNKNOWN.",
            "Historical pricing is optional; missing history is UNKNOWN, not a bid blocker by itself.",
        ],
    }
