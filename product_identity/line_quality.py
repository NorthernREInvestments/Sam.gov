"""Classify extracted rows: noise vs purchasing lines + quality gate."""

from __future__ import annotations

import re
from typing import Any

from product_identity.models import (
    BOILERPLATE,
    DUPLICATE,
    HEADER,
    INSTRUCTION_TEXT,
    LINE_ITEM_AMBIGUOUS,
    LINE_ITEM_CONFIRMED,
    LINE_ITEM_LIKELY,
    NOT_LINE_ITEM,
    OTHER_NOISE,
    REAL_PURCHASING_LINE,
    SPEC_TEXT,
    TABLE_FRAGMENT,
)

_HEADER = re.compile(
    r"\b(item|line|clin|description|qty|quantity|uom|unit\s*price|extended|manufacturer|model|part\s*#?)\b",
    re.I,
)
_BOILER = re.compile(
    r"\b("
    r"terms\s+and\s+conditions|general\s+provisions|indemnif|"
    r"insurance\s+requirements|equal\s+opportunity|page\s+\d+\s+of|"
    r"proprietary\s+and\s+confidential|all\s+rights\s+reserved|"
    r"by\s+signing\s+below|not\s+to\s+exceed|force\s+majeure"
    r")\b",
    re.I,
)
_INSTR = re.compile(
    r"\b(instructions?:|bidders?\s+shall|vendor\s+must|submit\s+proposal|"
    r"complete\s+the\s+following|see\s+attachment|refer\s+to\s+section)\b",
    re.I,
)
_SPEC = re.compile(
    r"\b(shall\s+be|must\s+meet|specification\s+section|as\s+per\s+drawing|"
    r"comply\s+with|applicable\s+codes?|workmanship)\b",
    re.I,
)
_VAGUE = re.compile(r"^(tools?|supplies?|equipment|materials?|misc\.?|various|chair|valve)$", re.I)


def classify_extraction_row(row: dict[str, Any], *, seen_keys: set[str] | None = None) -> str:
    desc = str(row.get("description") or row.get("product_description") or row.get("raw_description") or "").strip()
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("description", "manufacturer", "model", "part_number", "catalog_number", "item")
    )
    if not desc and not row.get("part_number") and not row.get("catalog_number") and not row.get("model"):
        return OTHER_NOISE
    if _BOILER.search(blob):
        return BOILERPLATE
    if _INSTR.search(blob) and not row.get("quantity") and not row.get("part_number"):
        return INSTRUCTION_TEXT
    # Header-like: many header tokens, no qty/pn
    if _HEADER.search(desc) and not row.get("quantity") and not row.get("part_number"):
        tokens = {t.lower() for t in re.findall(r"[a-z]+", desc.lower())}
        header_hits = tokens & {"item", "description", "qty", "quantity", "uom", "price", "manufacturer", "model"}
        if len(header_hits) >= 2:
            return HEADER
    if len(desc) > 220 and _SPEC.search(desc) and not row.get("quantity"):
        return SPEC_TEXT
    if seen_keys is not None:
        key = "|".join(
            [
                str(row.get("item") or ""),
                str(row.get("part_number") or row.get("catalog_number") or ""),
                desc[:80].lower(),
                str(row.get("quantity") or ""),
            ]
        )
        if key in seen_keys:
            return DUPLICATE
        seen_keys.add(key)
    # Thin fragment
    if len(desc) < 4 and not row.get("part_number"):
        return TABLE_FRAGMENT

    signals = 0
    if row.get("item") not in (None, ""):
        signals += 1
    if row.get("quantity") not in (None, ""):
        signals += 1
    if row.get("uom") not in (None, ""):
        signals += 1
    if row.get("part_number") or row.get("catalog_number") or row.get("model") or row.get("sku") or row.get("nsn"):
        signals += 2
    if row.get("manufacturer") or row.get("brand"):
        signals += 1
    if row.get("unit_price") not in (None, ""):
        signals += 1
    if desc and len(desc) >= 8 and not _VAGUE.match(desc):
        signals += 1
    if signals >= 2:
        return REAL_PURCHASING_LINE
    if signals == 1 and desc and len(desc) >= 12:
        return REAL_PURCHASING_LINE
    return OTHER_NOISE


def line_quality_gate(row: dict[str, Any], extraction_class: str) -> tuple[str, float, list[str]]:
    reasons: list[str] = []
    if extraction_class != REAL_PURCHASING_LINE:
        return NOT_LINE_ITEM, 0.0, [f"extraction:{extraction_class}"]

    score = 0.0
    if row.get("item") not in (None, ""):
        score += 0.15
        reasons.append("item_number")
    if row.get("quantity") not in (None, ""):
        score += 0.2
        reasons.append("quantity")
    if row.get("uom") not in (None, ""):
        score += 0.1
        reasons.append("uom")
    desc = str(row.get("description") or "").strip()
    if len(desc) >= 8:
        score += 0.2
        reasons.append("description")
    if row.get("part_number") or row.get("catalog_number") or row.get("sku") or row.get("nsn"):
        score += 0.25
        reasons.append("part_or_catalog")
    if row.get("model"):
        score += 0.15
        reasons.append("model")
    if row.get("manufacturer") or row.get("brand"):
        score += 0.15
        reasons.append("manufacturer")
    if row.get("unit_price") not in (None, ""):
        score += 0.1
        reasons.append("unit_price")

    if score >= 0.55 and ("part_or_catalog" in reasons or "model" in reasons or ("description" in reasons and "quantity" in reasons)):
        return LINE_ITEM_CONFIRMED, min(1.0, score), reasons
    if score >= 0.35:
        return LINE_ITEM_LIKELY, score, reasons
    if score >= 0.2:
        return LINE_ITEM_AMBIGUOUS, score, reasons
    return NOT_LINE_ITEM, score, reasons
