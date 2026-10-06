"""Freight / delivery economics for line-item contracts."""

from __future__ import annotations

from typing import Any

from line_item_economics.models import FREIGHT_UNKNOWN, LTL, OVERSIZED, PARCEL, TRUCKLOAD


def classify_freight_mode(
    *,
    estimated_weight_lb: float | None = None,
    oversized: bool = False,
    line_count: int = 0,
    explicit_mode: str | None = None,
) -> str:
    if explicit_mode:
        m = explicit_mode.strip().lower()
        if m in {PARCEL, LTL.lower(), TRUCKLOAD, OVERSIZED, FREIGHT_UNKNOWN}:
            return LTL if m == "ltl" else m
        if m == "tl":
            return TRUCKLOAD
    if oversized:
        return OVERSIZED
    if estimated_weight_lb is not None:
        if estimated_weight_lb <= 150:
            return PARCEL
        if estimated_weight_lb <= 10000:
            return LTL
        return TRUCKLOAD
    if line_count >= 30:
        return LTL
    return FREIGHT_UNKNOWN


def build_freight_block(
    *,
    estimated_weight_lb: float | None = None,
    dimensions: str | None = None,
    delivery_zip: str | None = None,
    delivery_location: str | None = None,
    public_shipping_quote: float | None = None,
    supplier_included_freight: bool | None = None,
    fob_terms: str | None = None,
    line_count: int = 0,
    oversized: bool = False,
    explicit_mode: str | None = None,
) -> dict[str, Any]:
    mode = classify_freight_mode(
        estimated_weight_lb=estimated_weight_lb,
        oversized=oversized,
        line_count=line_count,
        explicit_mode=explicit_mode,
    )
    estimate = public_shipping_quote
    if estimate is None and supplier_included_freight:
        estimate = 0.0
    return {
        "mode": mode,
        "estimated_weight_lb": estimated_weight_lb,
        "dimensions": dimensions,
        "delivery_zip": delivery_zip,
        "delivery_location": delivery_location,
        "public_shipping_quote": public_shipping_quote,
        "supplier_included_freight": supplier_included_freight,
        "fob_terms": fob_terms,
        "estimated_freight": estimate,
        "freight_known": estimate is not None,
        "note": None
        if estimate is not None
        else "Freight unknown — do not treat spread as post-freight profit.",
    }


def classify_lead_time(
    *,
    stock_status: str | None,
    lead_time_days: float | None,
    required_delivery_date: str | None,
    days_until_required: float | None = None,
) -> dict[str, Any]:
    from line_item_economics.models import LEAD_TIME_FAIL, LEAD_TIME_OK, LEAD_TIME_RISK, LEAD_TIME_UNKNOWN

    status = str(stock_status or "").upper()
    if status in {"IN_STOCK", "IN STOCK", "AVAILABLE"}:
        flag = LEAD_TIME_OK
    elif status in {"BACKORDER", "BACKORDERED", "OUT_OF_STOCK"}:
        flag = LEAD_TIME_FAIL if (days_until_required is not None and lead_time_days and lead_time_days > days_until_required) else LEAD_TIME_RISK
    elif lead_time_days is not None and days_until_required is not None:
        if lead_time_days <= days_until_required * 0.7:
            flag = LEAD_TIME_OK
        elif lead_time_days <= days_until_required:
            flag = LEAD_TIME_RISK
        else:
            flag = LEAD_TIME_FAIL
    else:
        flag = LEAD_TIME_UNKNOWN
    return {
        "stock_status": stock_status or "UNKNOWN",
        "lead_time_days": lead_time_days,
        "required_delivery_date": required_delivery_date,
        "flag": flag,
    }
