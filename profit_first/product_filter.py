"""Tangible-product primary filter — economics decide profitability, not category dislike."""

from __future__ import annotations

import re
from typing import Any

_SERVICE = re.compile(
    r"\b(consulting|staffing|temporary\s+help|labor\s+only|installation\s+only|"
    r"construction\s+services|janitorial\s+services|grounds?\s+maintenance|"
    r"professional\s+services|software\s+as\s+a\s+service|saas|managed\s+services|"
    r"engineering\s+services|custom\s+software\s+development)\b",
    re.I,
)
_PRODUCT = re.compile(
    r"\b(supply|supplies|equipment|parts?|tools?|furniture|hardware|widget|"
    r"nsn|mpn|sku|catalog|commodity|goods|materials?|ppe|glove|valve|pipe|"
    r"filter|lamp|bulb|battery|cable|hose|pump|motor|bearing|fastener|"
    r"paper|towel|cleaner|chemicals?|lumber|steel|wire)\b",
    re.I,
)
_MIXED = re.compile(r"\b(furnish\s+and\s+install|supply\s+and\s+install|product\s+and\s+service)\b", re.I)


def classify_tangible_product(rec: dict[str, Any] | None = None, *, title: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Primary filter: tangible product vs pure service. Categories are ranking signals only."""
    rec = rec or {}
    text = " ".join(
        str(x or "")
        for x in (
            title,
            description,
            rec.get("title"),
            rec.get("description"),
            rec.get("product_service_classification"),
            (rec.get("row_ref") or {}).get("title") if isinstance(rec.get("row_ref"), dict) else None,
        )
    )
    cls = str(rec.get("product_service_classification") or rec.get("universe_class") or "").upper()
    if cls in {"SERVICE", "PURE_SERVICE"}:
        return {
            "is_tangible_product": False,
            "class": "PURE_SERVICE",
            "action": "REJECT_OR_DEPRIORITIZE",
            "reason": "classified_as_service",
        }
    if cls in {"CONSTRUCTION"}:
        return {
            "is_tangible_product": False,
            "class": "CONSTRUCTION",
            "action": "REJECT_OR_DEPRIORITIZE",
            "reason": "classified_as_construction",
        }
    if cls in {
        "PRODUCT",
        "CORE_PRODUCT",
        "PRODUCT_RESALE",
        "TANGIBLE_PRODUCT",
        "PRODUCT_PLUS_SERVICE",
        "MIXED_PRODUCT_SERVICE",
        "MIXED_PRODUCT",
    }:
        return {
            "is_tangible_product": True,
            "class": (
                "MIXED_PRODUCT"
                if "MIXED" in cls or "PLUS_SERVICE" in cls or "PRODUCT_PLUS" in cls
                else "TANGIBLE_PRODUCT"
            ),
            "action": "CONTINUE",
            "reason": "classified_as_product",
        }

    has_product = bool(_PRODUCT.search(text))
    has_service = bool(_SERVICE.search(text))
    mixed = bool(_MIXED.search(text))

    if has_product and (not has_service or mixed):
        return {
            "is_tangible_product": True,
            "class": "MIXED_PRODUCT" if (has_service or mixed) else "TANGIBLE_PRODUCT",
            "action": "CONTINUE",
            "reason": "product_tokens",
        }
    if has_service and not has_product:
        return {
            "is_tangible_product": False,
            "class": "PURE_SERVICE",
            "action": "REJECT_OR_DEPRIORITIZE",
            "reason": "service_tokens_no_product",
        }
    if not text.strip():
        return {
            "is_tangible_product": None,
            "class": "UNKNOWN",
            "action": "HOLD_UNKNOWN",
            "reason": "insufficient_text",
        }
    # Unknown — do not auto-reject; research may reveal product
    return {
        "is_tangible_product": None,
        "class": "UNKNOWN",
        "action": "HOLD_UNKNOWN",
        "reason": "ambiguous",
    }
