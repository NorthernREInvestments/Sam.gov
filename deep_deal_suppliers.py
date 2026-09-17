"""Supplier research architecture + contact packets — no automatic outreach."""

from __future__ import annotations
from application_clock import now_utc

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from deep_deal_constants import EV_ASSESSMENT, EV_UNKNOWN, EV_VERIFIED_PUBLIC


def _utc() -> str:
    return now_utc().isoformat()


def build_line_item_record(
    *,
    line_number: str | None = None,
    description: str | None = None,
    manufacturer: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    part_number: str | None = None,
    quantity: float | None = None,
    uom: str | None = None,
    required_specification: str | None = None,
    acceptable_alternate: str | None = None,
    mandatory_accessory: str | None = None,
    warranty: str | None = None,
    delivery_location: str | None = None,
    delivery_deadline: str | None = None,
    installation_requirement: bool | None = None,
    source_document: str | None = None,
    product_certainty: str = "PRODUCT_UNKNOWN",
) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "description": description,
        "manufacturer": manufacturer,
        "brand": brand,
        "model": model,
        "part_number": part_number,
        "quantity": quantity,
        "uom": uom,
        "required_specification": required_specification,
        "acceptable_alternate": acceptable_alternate,
        "mandatory_accessory": mandatory_accessory,
        "warranty": warranty,
        "delivery_location": delivery_location,
        "delivery_deadline": delivery_deadline,
        "installation_requirement": installation_requirement,
        "source_document": source_document,
        "product_certainty": product_certainty,
    }


def supplier_candidate(
    *,
    supplier: str,
    website: str | None = None,
    phone: str | None = None,
    product: str | None = None,
    manufacturer: str | None = None,
    part_model: str | None = None,
    advertised_price: float | None = None,
    price_date: str | None = None,
    stock: str | None = None,
    lead_time: str | None = None,
    shipping_terms: str | None = None,
    government_sales_capability: bool | None = None,
    authorized_status: bool | None = None,
    direct_ship: bool | None = None,
    payment_terms: str | None = None,
    quote_required: bool = True,
    source_url: str | None = None,
    evidence_status: str = EV_UNKNOWN,
    supplier_class: str = "ESTABLISHED_DISTRIBUTOR",
) -> dict[str, Any]:
    """One supplier candidate — marketplace/auction not primary by default."""
    return {
        "supplier": supplier,
        "supplier_class": supplier_class,
        "website": website,
        "phone": phone,
        "product": product,
        "manufacturer": manufacturer,
        "part_model": part_model,
        "advertised_price": advertised_price,
        "price_type": "DISTRIBUTOR_ADVERTISED" if advertised_price is not None else None,
        "price_date": price_date,
        "stock_availability": stock,
        "lead_time": lead_time,
        "shipping_terms": shipping_terms,
        "government_sales_capability": government_sales_capability,
        "authorized_status": authorized_status,
        "direct_ship_capability": direct_ship,
        "payment_terms": payment_terms,
        "quote_required": quote_required,
        "source_url": source_url,
        "evidence_status": evidence_status,
        "not_primary_if": ["random_marketplace", "used_auction", "unverified_reseller"],
    }


def public_supplier_search_plan(line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plan public search queries — does not execute HTTP."""
    plans = []
    for item in line_items or []:
        parts = [item.get("manufacturer"), item.get("brand"), item.get("model"), item.get("part_number"), item.get("description")]
        q = " ".join(str(p) for p in parts if p)
        if not q or len(q) < 6:
            continue
        plans.append(
            {
                "query": q[:200],
                "prefer": ["manufacturer", "authorized_distributor", "industrial_distributor"],
                "avoid": ["auction", "used_only_marketplace"],
                "line_number": item.get("line_number"),
                "search_url_template": f"https://www.google.com/search?q={quote_plus(q[:120])}",
            }
        )
    return plans


def build_supplier_contact_packet(
    *,
    opportunity: dict[str, Any],
    line_items: list[dict[str, Any]] | None = None,
    suppliers: list[dict[str, Any]] | None = None,
    delivery_location: str | None = None,
    delivery_deadline: str | None = None,
) -> dict[str, Any]:
    """Operator packet for one useful supplier call — no auto-contact."""
    items = line_items or []
    primary = (items[0] if items else {}) or {}
    product_desc = primary.get("description") or opportunity.get("title") or "product per solicitation"
    qty = primary.get("quantity")
    part = primary.get("part_number") or primary.get("model")

    questions = [
        "Price for the required quantity (firm quote)?",
        "Freight / shipping cost to the delivery location?",
        "Lead time from PO / stock availability?",
        "Quote validity period?",
        "Warranty terms included?",
        "Can you direct-ship to the government customer?",
        "Is a deposit required? If so, how much?",
        "Is full prepayment required before shipment?",
        "Are Net terms available? Under what conditions?",
        "Will you accept direct payment from a PO / government-contract financier?",
        "Can you offer project-specific terms based on a government PO?",
        "Are you an authorized distributor for this manufacturer (if applicable)?",
    ]

    quote_request = (
        f"Please provide a firm commercial quote for: {product_desc}"
        + (f", part/model {part}" if part else "")
        + (f", quantity {qty}" if qty else "")
        + (f". Delivery to: {delivery_location}." if delivery_location else ".")
        + (f" Required delivery by: {delivery_deadline}." if delivery_deadline else "")
        + " Include unit price, extended price, freight, lead time, warranty, and payment terms."
    )

    contacts = []
    for s in suppliers or []:
        contacts.append(
            {
                "supplier": s.get("supplier"),
                "who_or_department": "Government / Contract Sales or Inside Sales",
                "phone": s.get("phone"),
                "website": s.get("website"),
                "product_part": s.get("part_model") or part,
                "quantity": qty,
                "delivery_location": delivery_location,
                "delivery_deadline": delivery_deadline,
                "exact_quote_request": quote_request,
                "questions_to_ask": questions,
            }
        )

    if not contacts:
        contacts.append(
            {
                "supplier": "TBD — identify authorized distributor",
                "who_or_department": "Government / Contract Sales or Inside Sales",
                "phone": None,
                "website": None,
                "product_part": part,
                "quantity": qty,
                "delivery_location": delivery_location,
                "delivery_deadline": delivery_deadline,
                "exact_quote_request": quote_request,
                "questions_to_ask": questions,
            }
        )

    return {
        "solicitation": opportunity.get("solicitation_number") or opportunity.get("solicitation_id"),
        "agency": opportunity.get("agency"),
        "suppliers_to_contact": contacts,
        "outreach_performed": False,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def commercial_price_benchmark(
    *,
    amount: float | None,
    price_type: str,
    source: str | None = None,
    date: str | None = None,
    quantity_basis: float | None = None,
) -> dict[str, Any]:
    """Separate price types — never mix into one generic price."""
    allowed = {
        "MSRP",
        "MANUFACTURER_LIST",
        "DISTRIBUTOR_ADVERTISED",
        "RETAIL",
        "GOVERNMENT_CONTRACT_PRICE",
        "SUPPLIER_QUOTE",
        "HISTORICAL_AWARD_UNIT_PRICE",
    }
    if price_type not in allowed:
        raise ValueError(f"price_type must be one of {sorted(allowed)}")
    return {
        "price_type": price_type,
        "amount": amount,
        "date": date or _utc()[:10],
        "source": source,
        "quantity_basis": quantity_basis,
        "verification_status": EV_VERIFIED_PUBLIC if amount is not None and source else EV_UNKNOWN,
    }


def historical_pricing_record(
    *,
    historical_customer: str | None = None,
    date: str | None = None,
    product_category: str | None = None,
    quantity: float | None = None,
    award_value: float | None = None,
    unit_price: float | None = None,
    awardee: str | None = None,
    source: str | None = None,
    similarity: str = "UNKNOWN",
    confidence: str = "LOW",
) -> dict[str, Any]:
    """Historical award is evidence, not guaranteed current value."""
    derived_unit = unit_price
    if derived_unit is None and award_value is not None and quantity and quantity > 0:
        derived_unit = round(float(award_value) / float(quantity), 2)
    elif unit_price is None and (quantity is None or not quantity):
        derived_unit = None  # do not invent unit price

    return {
        "historical_customer": historical_customer,
        "date": date,
        "product_category": product_category,
        "quantity": quantity,
        "award_value": award_value,
        "unit_price": derived_unit,
        "unit_price_note": None if derived_unit is not None or award_value is None else "unit_price_not_derived_quantity_unknown",
        "awardee": awardee,
        "source": source,
        "similarity": similarity,
        "confidence": confidence,
        "is_current_deal_revenue": False,
        "verification_status": EV_ASSESSMENT if award_value is not None else EV_UNKNOWN,
    }


def is_primary_sourcing_class(supplier_class: str | None) -> bool:
    primary = {
        "MANUFACTURER",
        "AUTHORIZED_DISTRIBUTOR",
        "INDUSTRIAL_DISTRIBUTOR",
        "ESTABLISHED_DISTRIBUTOR",
        "COMMERCIAL_SUPPLIER",
        "SPECIALTY_DISTRIBUTOR",
        "CREDIBLE_DEALER",
    }
    return (supplier_class or "").upper() in primary
