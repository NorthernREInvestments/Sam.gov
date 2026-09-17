"""Supplier quote validation — totals never become acquisition cost without match."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime
from typing import Any

QUOTE_VALID = "QUOTE_VALID"
QUOTE_INCOMPLETE = "QUOTE_INCOMPLETE"
QUOTE_MISMATCH = "QUOTE_MISMATCH"
QUOTE_EXPIRED = "QUOTE_EXPIRED"
QUOTE_UNVERIFIED = "QUOTE_UNVERIFIED"


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def validate_supplier_quote(
    quote: dict[str, Any] | None,
    *,
    required_bom: list[dict[str, Any]] | None = None,
    required_quantity: float | int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """
    QUOTE_VALID only when verified, unexpired, BOM/qty match, and commercial fields present.
    """
    if not isinstance(quote, dict) or not quote:
        return {
            "status": QUOTE_INCOMPLETE,
            "usable_as_acquisition_cost": False,
            "blockers": ["quote_missing"],
        }

    blockers: list[str] = []
    verification = str(quote.get("verification_status") or quote.get("status") or "UNKNOWN").upper()
    if verification not in {"VERIFIED", "OPERATOR_CONFIRMED", "VERIFIED_FROM_DOCUMENT"}:
        blockers.append("quote_unverified")

    exp = _parse_date(quote.get("expiration") or quote.get("expiration_date"))
    as_of = today or today_local()
    if exp is not None and exp < as_of:
        return {
            "status": QUOTE_EXPIRED,
            "usable_as_acquisition_cost": False,
            "blockers": ["quote_expired"],
            "expiration": exp.isoformat(),
        }

    qty = quote.get("quantity")
    if qty is None:
        qty = quote.get("quantity_basis")
    if required_quantity is not None:
        try:
            if qty is None or float(qty) != float(required_quantity):
                blockers.append("quantity_mismatch")
        except (TypeError, ValueError):
            blockers.append("quantity_unparseable")

    # BOM match: if quote declares bom_match explicitly
    bom_match = quote.get("bom_match")
    if bom_match is False:
        blockers.append("bom_mismatch")
    elif bom_match is None and required_bom:
        # Without affirmative match evidence, cannot treat as valid acquisition cost
        quote_bom = quote.get("exact_bom") or quote.get("bom")
        if not quote_bom:
            blockers.append("bom_match_unverified")
        elif isinstance(quote_bom, list) and required_bom:
            req_keys = {
                str(i.get("component") or i.get("key"))
                for i in required_bom
                if str(i.get("status") or "").upper() == "VERIFIED"
            }
            got_keys = {
                str(i.get("component") or i.get("key"))
                for i in quote_bom
                if isinstance(i, dict)
            }
            if req_keys and not req_keys.issubset(got_keys):
                blockers.append("bom_mismatch")

    unit = quote.get("unit_price")
    ext = quote.get("extended_price") or quote.get("total")
    if unit is None and ext is None:
        blockers.append("price_missing")

    if "quote_unverified" in blockers and len(blockers) == 1:
        return {
            "status": QUOTE_UNVERIFIED,
            "usable_as_acquisition_cost": False,
            "blockers": blockers,
        }

    if any(b.startswith("bom_") or b == "quantity_mismatch" for b in blockers):
        return {
            "status": QUOTE_MISMATCH,
            "usable_as_acquisition_cost": False,
            "blockers": blockers,
        }

    if blockers:
        return {
            "status": QUOTE_INCOMPLETE if "price_missing" in blockers else QUOTE_UNVERIFIED,
            "usable_as_acquisition_cost": False,
            "blockers": blockers,
        }

    return {
        "status": QUOTE_VALID,
        "usable_as_acquisition_cost": True,
        "blockers": [],
        "unit_price": unit,
        "extended_price": ext,
        "expiration": exp.isoformat() if exp else None,
    }


NEGOTIATION_SUGGESTIONS = (
    "ask for volume pricing",
    "ask for government/public-sector pricing",
    "ask for project/deal-registration pricing",
    "ask whether manufacturer special pricing is available",
    "ask for competitive bid pricing",
    "ask for freight included",
    "ask for price protection through expected award",
    "ask for quote validity extension",
    "ask for improved payment terms",
    "ask for supplier-direct fulfillment",
    "ask for government PO terms",
    "ask for escalation to public-sector/account manager",
    "ask whether another authorized distribution path can produce better pricing",
)


def default_negotiation_suggestions(*, kind: str = "product_resale") -> list[dict[str, Any]]:
    """Suggested operator actions — never facts."""
    return [
        {
            "suggestion": s,
            "kind": "OPERATOR_ACTION_SUGGESTION",
            "status": "SUGGESTION",
            "is_fact": False,
        }
        for s in NEGOTIATION_SUGGESTIONS
    ]
