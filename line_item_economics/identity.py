"""Per-line product identity classification."""

from __future__ import annotations

import re
from typing import Any

from line_item_economics.models import (
    BRAND_OR_EQUAL,
    EXACT_MODEL,
    EXACT_NSN,
    EXACT_PART_NUMBER,
    GENERIC_SPEC,
    OR_EQUAL_YES,
    PARTIAL_IDENTITY,
    UNKNOWN_IDENTITY,
)

_GENERIC = re.compile(
    r"\b(various|assorted|misc|miscellaneous|catalog|supplies|as\s+needed|tbd)\b",
    re.I,
)


def classify_identity(line: dict[str, Any]) -> str:
    """Classify identity. Never assume similar descriptions are equal."""
    nsn = str(line.get("nsn") or "").strip()
    pn = str(line.get("part_number") or "").strip()
    model = str(line.get("model") or "").strip()
    brand = str(line.get("brand") or line.get("requested_brand") or line.get("manufacturer") or "").strip()
    desc = str(line.get("product_description") or line.get("original_text") or "").strip()
    or_eq = str(line.get("or_equal_allowed") or "").upper()
    salient = str(line.get("salient_characteristics") or "").strip()

    if nsn and re.search(r"\d", nsn):
        line["identity_class"] = EXACT_NSN
        return EXACT_NSN
    if pn and len(pn) >= 3:
        line["identity_class"] = EXACT_PART_NUMBER
        return EXACT_PART_NUMBER
    if model and brand:
        line["identity_class"] = EXACT_MODEL
        return EXACT_MODEL
    if brand and or_eq == OR_EQUAL_YES:
        line["identity_class"] = BRAND_OR_EQUAL
        if not line.get("salient_characteristics") and desc:
            line["salient_characteristics"] = desc[:500]
        return BRAND_OR_EQUAL
    if brand and model:
        line["identity_class"] = EXACT_MODEL
        return EXACT_MODEL
    if brand and not model and or_eq != OR_EQUAL_YES:
        line["identity_class"] = PARTIAL_IDENTITY
        return PARTIAL_IDENTITY
    if salient or (desc and len(desc) > 20 and not _GENERIC.search(desc)):
        if or_eq == OR_EQUAL_YES or not brand:
            line["identity_class"] = GENERIC_SPEC
            if not line.get("salient_characteristics"):
                line["salient_characteristics"] = (salient or desc)[:500]
            return GENERIC_SPEC
        line["identity_class"] = PARTIAL_IDENTITY
        return PARTIAL_IDENTITY
    line["identity_class"] = UNKNOWN_IDENTITY
    return UNKNOWN_IDENTITY


def classify_all(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for line in lines:
        classify_identity(line)
    return lines
