"""R2 supplier evidence — consume L.22/quotes; never fabricate prices."""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

from response_engine.firewall import ingest_supplier_quote_as_internal
from response_engine.models import new_id
from response_engine.money import D, as_str, money
from response_engine.r2_constants import (
    HISTORICAL_INTERNAL,
    UNKNOWN,
    VERBAL_QUOTE,
    VERIFIED,
    WEB_PRICE,
    WRITTEN_QUOTE,
)


def classify_quote_type(quote: dict[str, Any]) -> str:
    t = str(quote.get("quote_type") or quote.get("type") or "").upper()
    if t in {WRITTEN_QUOTE, VERBAL_QUOTE, WEB_PRICE, "CATALOG_PRICE", "DISTRIBUTOR_PORTAL_PRICE", "ESTIMATE", HISTORICAL_INTERNAL}:
        return t
    blob = str(quote).lower()
    if quote.get("quote_file") or quote.get("written") or "written" in blob:
        return WRITTEN_QUOTE
    if "verbal" in blob or quote.get("verbal"):
        return VERBAL_QUOTE
    if "http" in blob or "web" in blob:
        return WEB_PRICE
    if "historical" in blob:
        return HISTORICAL_INTERNAL
    return UNKNOWN


def quote_supports_verified_acquisition(quote: dict[str, Any]) -> bool:
    """Written / portal price only — historical discount is NOT verified acquisition."""
    qt = classify_quote_type(quote)
    if qt not in {WRITTEN_QUOTE, "DISTRIBUTOR_PORTAL_PRICE"}:
        return False
    if D(quote.get("unit_price")) is None:
        return False
    if quote_staleness(quote).get("status") == "EXPIRED":
        return False
    return True


def quote_staleness(quote: dict[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
    exp = quote.get("quote_expiration") or quote.get("expiration") or quote.get("valid_until")
    if not exp:
        return {"status": "UNKNOWN", "flag": None}
    # Simple ISO date compare when possible
    try:
        from datetime import datetime

        exp_d = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        now = datetime.fromisoformat((as_of or now_utc().isoformat()).replace("Z", "+00:00"))
        days = (exp_d - now).days
        if days < 0:
            return {"status": "EXPIRED", "flag": "QUOTE_VALIDITY_RISK", "days": days}
        if days <= 7:
            return {"status": "EXPIRING_SOON", "flag": "QUOTE_VALIDITY_RISK", "days": days}
        return {"status": "CURRENT", "flag": None, "days": days}
    except Exception:
        return {"status": "UNKNOWN", "flag": "QUOTE_VALIDITY_RISK"}


def extract_quote_conditions(quote: dict[str, Any]) -> list[str]:
    blob = str(quote).lower()
    flags = []
    mapping = [
        ("subject to change", "PRICE_SUBJECT_TO_CHANGE"),
        ("subject to availability", "SUBJECT_TO_AVAILABILITY"),
        ("freight excluded", "FREIGHT_EXCLUDED"),
        ("freight not included", "FREIGHT_EXCLUDED"),
        ("minimum order", "MINIMUM_ORDER"),
        ("deposit", "DEPOSIT"),
        ("prepay", "PREPAYMENT"),
        ("personal guarantee", "PERSONAL_GUARANTEE"),
        ("noncancelable", "NONCANCELABLE"),
        ("no returns", "NO_RETURNS"),
    ]
    for needle, flag in mapping:
        if needle in blob:
            flags.append(flag)
    return flags


def attach_supplier_quote(project: dict[str, Any], quote: dict[str, Any], *, line_item_id: str | None = None) -> dict[str, Any]:
    """Store quote as INTERNAL evidence + R2 supplier_quotes list. Never fabricates."""
    if D(quote.get("unit_price")) is None and D(quote.get("extended_price")) is None:
        return {"ok": False, "error": "no_price_in_quote", "fabricated": False}
    qt = classify_quote_type(quote)
    conditions = extract_quote_conditions(quote)
    stale = quote_staleness(quote)
    row = {
        "kind": "SupplierQuoteEvidence",
        "quote_evidence_id": new_id("SQ"),
        "line_item_id": line_item_id,
        "supplier": quote.get("supplier"),
        "quote_number": quote.get("quote_number"),
        "quote_date": quote.get("quote_date"),
        "quote_expiration": quote.get("quote_expiration") or quote.get("expiration"),
        "quote_type": qt,
        "quantity": quote.get("quantity"),
        "supplier_uom": quote.get("uom") or quote.get("supplier_uom"),
        "unit_price": as_str(money(D(quote.get("unit_price")))) if D(quote.get("unit_price")) is not None else None,
        "extended_price": as_str(money(D(quote.get("extended_price")))) if D(quote.get("extended_price")) is not None else None,
        "freight": quote.get("freight"),
        "lead_time": quote.get("lead_time") or quote.get("delivery"),
        "payment_terms": quote.get("payment_terms") or quote.get("terms"),
        "condition": quote.get("condition"),
        "country_of_origin": quote.get("country_of_origin"),
        "warranty": quote.get("warranty"),
        "conditions": conditions,
        "staleness": stale,
        "supports_verified_acquisition": quote_supports_verified_acquisition({**quote, "quote_type": qt}),
        "evidence_state": VERIFIED if quote_supports_verified_acquisition({**quote, "quote_type": qt}) else UNKNOWN,
        "namespace": "INTERNAL_EVIDENCE",
        "may_enter_submission": False,
    }
    project.setdefault("supplier_quotes", []).append(row)
    ingest_supplier_quote_as_internal(project, {**quote, "conditions": conditions})
    return {"ok": True, "quote": row}


def build_supplier_questions_for_gaps(gaps: list[str], *, product_hint: str | None = None) -> list[str]:
    """Feed L.22 — missing evidence questions. Never include max-buy / historical / margin."""
    qmap = {
        "freight": "Please confirm freight cost to the delivery destination, or confirm freight is included.",
        "coo": "Please confirm country of origin / country of manufacture.",
        "warranty": "Please confirm warranty terms.",
        "lead_time": "Please confirm lead time / delivery days ARO.",
        "pack_size": "Please confirm case/pack quantity (units per case).",
        "authorization": "Please provide manufacturer authorization / authorized distributor evidence if applicable.",
        "quote_validity": "Please confirm quote validity / expiration date.",
        "mpn": "Please confirm exact manufacturer part number on the quote.",
    }
    forbidden = re.compile(r"max[\-\s]?buy|historical|margin|target profit|government paid|budget", re.I)
    out = []
    for g in gaps:
        text = qmap.get(g) or f"Please provide: {g}"
        if product_hint:
            text = f"Regarding {product_hint}: {text}"
        if forbidden.search(text):
            continue
        out.append(text)
    return out


def sanitize_supplier_facing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip internal economics before any supplier-facing artifact."""
    banned = {
        "max_buy", "internal_max_buy", "margin", "target_profit", "expected_profit",
        "historical_government_price", "gov_historical", "financing_ceiling",
        "supplier_quote_target", "thresholds",
    }
    return {k: v for k, v in payload.items() if k not in banned and not str(k).startswith("internal_")}
