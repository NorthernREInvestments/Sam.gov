"""Constants and empty schemas for line-item economics."""

from __future__ import annotations

from typing import Any

# Identity classes
EXACT_MODEL = "EXACT_MODEL"
EXACT_PART_NUMBER = "EXACT_PART_NUMBER"
EXACT_NSN = "EXACT_NSN"
BRAND_OR_EQUAL = "BRAND_OR_EQUAL"
GENERIC_SPEC = "GENERIC_SPEC"
PARTIAL_IDENTITY = "PARTIAL_IDENTITY"
UNKNOWN_IDENTITY = "UNKNOWN"

# Profit proof labels
RETAIL_PROFIT_PROVEN = "RETAIL_PROFIT_PROVEN"
RETAIL_PROFIT_LIKELY = "RETAIL_PROFIT_LIKELY"
RETAIL_PROFIT_UNKNOWN = "RETAIL_PROFIT_UNKNOWN"
RETAIL_NEGATIVE = "RETAIL_NEGATIVE"

# Buckets
GREEN_PLUS = "GREEN_PLUS"
GREEN = "GREEN"
YELLOW = "YELLOW"
YELLOW_PLUS = "YELLOW_PLUS"
RED = "RED"
UNKNOWN_BUCKET = "UNKNOWN"

# Completeness grades
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"
GRADE_D = "D"

# Lead time
LEAD_TIME_OK = "LEAD_TIME_OK"
LEAD_TIME_RISK = "LEAD_TIME_RISK"
LEAD_TIME_FAIL = "LEAD_TIME_FAIL"
LEAD_TIME_UNKNOWN = "UNKNOWN"

# Freight modes
PARCEL = "parcel"
LTL = "LTL"
TRUCKLOAD = "truckload"
OVERSIZED = "oversized"
FREIGHT_UNKNOWN = "unknown"

# Or-equal
OR_EQUAL_YES = "YES"
OR_EQUAL_NO = "NO"
OR_EQUAL_UNKNOWN = "UNKNOWN"

BUILD = "20261002-line-item-economics-retail-profit-proof"


def empty_line_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "LineItemEconomicsLine",
        "line_id": None,
        "clin": None,
        "line_number": None,
        "product_description": None,
        "manufacturer": None,
        "brand": None,
        "model": None,
        "part_number": None,
        "nsn": None,
        "upc": None,
        "size": None,
        "color": None,
        "material": None,
        "pack_size": None,
        "unit_of_measure": None,
        "quantity": None,
        "requested_brand": None,
        "or_equal_allowed": OR_EQUAL_UNKNOWN,
        "salient_characteristics": None,
        "delivery_location": None,
        "delivery_date": None,
        "line_notes": None,
        "original_text": None,
        "provenance": [],
        "identity_class": UNKNOWN_IDENTITY,
        "retail": None,
        "retail_equal": None,
        "historical": None,
        "economics": None,
        "freight": None,
        "lead_time": None,
        "suppliers_covering": [],
        "resolved": False,
        "unresolved_reason": None,
    }
    base.update(overrides)
    return base
