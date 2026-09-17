"""Evidence maturity — estimates never silently become verified."""

from __future__ import annotations

from typing import Any

from executable_deal_constants import (
    EV_ESTIMATE,
    EV_INFERRED,
    EV_OPERATOR_CONFIRMED,
    EV_QUOTE_REQUIRED,
    EV_UNKNOWN,
    EV_VERIFIED_QUOTE,
    EV_VERIFIED_SOLICITATION,
)


VERIFIED_SET = frozenset(
    {
        EV_VERIFIED_SOLICITATION,
        EV_OPERATOR_CONFIRMED,
        EV_VERIFIED_QUOTE,
        "VERIFIED",
        "VERIFIED_PROVIDER",
    }
)


def mature_fact(
    value: Any,
    maturity: str,
    *,
    source: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    mat = str(maturity or EV_UNKNOWN)
    if value is None or value == "" or value == "UNKNOWN":
        # Preserve explicit quote-required / unknown distinction; never invent values.
        if mat not in {EV_QUOTE_REQUIRED, EV_UNKNOWN}:
            mat = EV_UNKNOWN
        if value == "UNKNOWN":
            value = None
    return {
        "value": value,
        "maturity": mat,
        "is_verified": mat in VERIFIED_SET and value is not None,
        "is_estimate": mat in {EV_ESTIMATE, EV_INFERRED},
        "quote_required": mat == EV_QUOTE_REQUIRED,
        "source": source,
        "notes": notes,
    }


def assert_not_promoting_estimate(fact: dict[str, Any]) -> dict[str, Any]:
    """Guard: estimates cannot be labeled verified."""
    out = dict(fact)
    if out.get("is_estimate") and out.get("is_verified"):
        out["is_verified"] = False
        out["maturity"] = EV_ESTIMATE
        out["notes"] = (out.get("notes") or "") + "|estimate_cannot_be_verified"
    return out


def freight_fact(value: Any = None, *, included_in_supplier: bool | None = None, maturity: str = EV_UNKNOWN) -> dict[str, Any]:
    """Freight must never silently equal zero unless explicitly verified zero."""
    if value is None and included_in_supplier is not True:
        return mature_fact(None, EV_QUOTE_REQUIRED if maturity == EV_UNKNOWN else maturity, notes="freight_unknown_not_zero")
    if value == 0 and maturity not in VERIFIED_SET and included_in_supplier is not True:
        return mature_fact(None, EV_UNKNOWN, notes="zero_freight_rejected_without_verification")
    if included_in_supplier:
        return mature_fact(0, maturity if maturity != EV_UNKNOWN else EV_OPERATOR_CONFIRMED, notes="freight_included_in_supplier_price")
    return mature_fact(value, maturity)
