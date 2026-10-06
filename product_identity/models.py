"""Product identity constants and empty schemas."""

from __future__ import annotations

from typing import Any

BUILD_TARGET = "20261003-m3-product-identity-v1"

# Raw extraction classes
REAL_PURCHASING_LINE = "REAL_PURCHASING_LINE"
HEADER = "HEADER"
BOILERPLATE = "BOILERPLATE"
SPEC_TEXT = "SPEC_TEXT"
INSTRUCTION_TEXT = "INSTRUCTION_TEXT"
DUPLICATE = "DUPLICATE"
TABLE_FRAGMENT = "TABLE_FRAGMENT"
OTHER_NOISE = "OTHER_NOISE"

# Line quality gate
LINE_ITEM_CONFIRMED = "LINE_ITEM_CONFIRMED"
LINE_ITEM_LIKELY = "LINE_ITEM_LIKELY"
LINE_ITEM_AMBIGUOUS = "LINE_ITEM_AMBIGUOUS"
NOT_LINE_ITEM = "NOT_LINE_ITEM"

# Identity types
EXACT_MPN = "EXACT_MPN"
EXACT_MODEL = "EXACT_MODEL"
EXACT_CATALOG_NUMBER = "EXACT_CATALOG_NUMBER"
EXACT_NSN = "EXACT_NSN"
EXACT_UPC = "EXACT_UPC"
PERMITTED_EQUAL = "PERMITTED_EQUAL"
STRONG_GENERIC_SPEC = "STRONG_GENERIC_SPEC"
WEAK_IDENTITY = "WEAK_IDENTITY"
NO_IDENTITY = "NO_IDENTITY"

USABLE_GRADES = {"A", "B", "C"}


def empty_identity_record(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "ProductIdentityRecord",
        "build_target": BUILD_TARGET,
        "line_id": None,
        "opportunity_id": None,
        "raw_description": None,
        "manufacturer": None,
        "manufacturer_raw": None,
        "brand": None,
        "model": None,
        "model_raw": None,
        "part_number": None,
        "part_number_raw": None,
        "catalog_number": None,
        "sku": None,
        "nsn": None,
        "upc": None,
        "quantity": None,
        "uom": None,
        "uom_normalized": None,
        "pack_size": None,
        "pack_note": None,
        "size": None,
        "dimensions": None,
        "material": None,
        "color": None,
        "attributes": {},
        "identity_type": NO_IDENTITY,
        "confidence_grade": "F",
        "line_quality": NOT_LINE_ITEM,
        "extraction_class": OTHER_NOISE,
        "equal_allowed": False,
        "reference_product": None,
        "reference_manufacturer": None,
        "reference_model": None,
        "commercial_search_key": None,
        "inherited_manufacturer": False,
        "enriched_from_spec": False,
        "brand_or_equal_context": False,
        "source_document": None,
        "source_kind": None,  # xlsx|xls|csv|pdf|docx
        "confidence_reasons": [],
        "provenance": [],
    }
    base.update(overrides)
    return base
