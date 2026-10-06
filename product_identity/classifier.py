"""Exact / permitted-equal / strong-generic identity classification + confidence."""

from __future__ import annotations

import re
from typing import Any

from product_identity.models import (
    EXACT_CATALOG_NUMBER,
    EXACT_MODEL,
    EXACT_MPN,
    EXACT_NSN,
    EXACT_UPC,
    NO_IDENTITY,
    PERMITTED_EQUAL,
    STRONG_GENERIC_SPEC,
    WEAK_IDENTITY,
    empty_identity_record,
)

_OR_EQUAL = re.compile(
    r"\b("
    r"brand\s+or\s+equal|or\s+approved\s+equal|or\s+equivalent|or\s+equal|"
    r"equal\s+to|basis\s+of\s+design|acceptable\s+manufacturers?"
    r")\b",
    re.I,
)
_LABELED_PN = re.compile(
    r"\b(?:MPN|P/?N|PART\s*NO\.?|PART\s*#|PART\s*NUMBER|MODEL(?:\s*NO\.?)?|CAT(?:ALOG)?\s*NO\.?|ITEM\s*NO\.?)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9\-./]{2,})\b",
    re.I,
)
_MIXED_TOKEN = re.compile(r"\b([A-Z]{1,6}\d[\w\-./]{2,}|\d{2,}[A-Z][\w\-./]{1,})\b", re.I)
_VAGUE = re.compile(
    r"^(tools?|supplies?|equipment|materials?|misc\.?|various|chair|valve|services?|labor|freight)$",
    re.I,
)
_STRONG_GENERIC_CUES = re.compile(
    r"\b("
    r"\d+\s*(?:ft|in|inch|lb|lbs|v|volt|w|watt|lumens?|ah|ga|gauge|mm|cm|oz)\b|"
    r"type\s+[iea]{1,3}|class\s+[123]|ansi|astm|ul\s*listed|nema|"
    r"fiberglass|cordless|led|diesel|gasoline|stainless|galvanized|"
    r"high\s+visibility|step\s+ladder|strip\s+fixture|chuck|batter(?:y|ies)"
    r")",
    re.I,
)
_NOT_PRODUCT = re.compile(
    r"\b("
    r"ft-c|foot-?candles?|lighting\s+requirement|insurance|liability|"
    r"indemnif|policy\s+limit|workers?\s+compensation|page\s+\d|"
    r"shall\s+be\s+no\s+less|minimum\s+limits"
    r")\b",
    re.I,
)
_STOP_MODELS = {
    "and", "or", "the", "has", "have", "available", "equal", "approved",
    "model", "part", "item", "with", "for", "from", "this", "that", "shall",
}


def _extract_labeled_from_desc(desc: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _LABELED_PN.finditer(desc or ""):
        label = m.group(0).split()[0].upper()
        token = m.group(1)
        if "MODEL" in label:
            out.setdefault("model", token)
        elif "CAT" in label:
            out.setdefault("catalog_number", token)
        else:
            out.setdefault("part_number", token)
    return out


def _strong_generic(desc: str) -> bool:
    if not desc or len(desc) < 24 or _VAGUE.match(desc.strip()):
        return False
    if _NOT_PRODUCT.search(desc):
        return False
    cues = len(_STRONG_GENERIC_CUES.findall(desc))
    # Need multiple commercial attributes on a product-like phrase
    return cues >= 2 or (cues >= 1 and len(desc) >= 40 and bool(re.search(r"\d", desc)))


def _plausible_token(tok: str | None) -> bool:
    if not tok:
        return False
    t = str(tok).strip()
    if len(t) < 3 or t.lower() in _STOP_MODELS:
        return False
    if not re.search(r"\d", t):
        return False
    if re.fullmatch(r"[A-Za-z]+", t):
        return False
    return True


def classify_identity(norm: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return identity record fields for a normalized purchasing line."""
    extra = extra or {}
    desc = str(norm.get("raw_description") or "")
    labeled = _extract_labeled_from_desc(desc)
    pn = norm.get("part_number") or labeled.get("part_number")
    cat = norm.get("catalog_number") or labeled.get("catalog_number")
    model = norm.get("model") or labeled.get("model")
    mfr = norm.get("manufacturer")
    # Reject prose manufacturers
    if mfr and (len(str(mfr).split()) > 6 or _NOT_PRODUCT.search(str(mfr)) or len(str(mfr)) > 48):
        mfr = None
    if pn and not _plausible_token(pn):
        pn = None
    if model and not _plausible_token(model):
        model = None
    if cat and not _plausible_token(cat):
        cat = None
    nsn = norm.get("nsn")
    upc = norm.get("upc")
    equal = bool(extra.get("equal_allowed")) or bool(_OR_EQUAL.search(desc)) or bool(extra.get("brand_or_equal_context"))
    if equal and _NOT_PRODUCT.search(desc):
        equal = False

    # Candidate model from mixed tokens only with manufacturer or label context
    if not model and not pn and mfr:
        m = _MIXED_TOKEN.search(desc)
        if m and mfr.lower() not in m.group(1).lower():
            tok = m.group(1)
            if _plausible_token(tok):
                model = tok.upper()
                extra = {**extra, "model_from_desc_token": True}

    reasons: list[str] = []
    identity = NO_IDENTITY
    grade = "F"

    if nsn:
        identity = EXACT_NSN
        grade = "A"
        reasons.append("nsn")
    elif upc and len(str(upc)) >= 12:
        identity = EXACT_UPC
        grade = "A"
        reasons.append("upc")
    elif pn and re.search(r"\d", str(pn)) and len(str(pn)) >= 3:
        identity = EXACT_MPN
        grade = "A" if mfr else "C"
        reasons.append("part_number")
        if mfr:
            reasons.append("manufacturer")
    elif cat and re.search(r"\d", str(cat)) and len(str(cat)) >= 3:
        identity = EXACT_CATALOG_NUMBER
        grade = "A" if mfr else "C"
        reasons.append("catalog_number")
        if mfr:
            reasons.append("manufacturer")
    elif model and mfr:
        identity = EXACT_MODEL
        grade = "A"
        reasons.append("manufacturer+model")
    elif model and re.search(r"\d", str(model)) and len(str(model)) >= 3 and desc and len(desc) >= 8:
        # Catalog-like model without manufacturer still commercially searchable
        identity = EXACT_MODEL
        grade = "C"
        reasons.append("model_number")
    elif equal and (mfr or model or pn or cat):
        identity = PERMITTED_EQUAL
        grade = "B"
        reasons.append("brand_or_equal")
    elif _strong_generic(desc):
        identity = STRONG_GENERIC_SPEC
        grade = "B"
        reasons.append("strong_generic_cues")
    elif desc and len(desc) >= 20 and not _VAGUE.match(desc.strip()):
        identity = WEAK_IDENTITY
        grade = "D"
        reasons.append("weak_description")
    else:
        identity = NO_IDENTITY
        grade = "F"
        reasons.append("insufficient_identity")

    # Upgrade weak model-only with equal language
    if identity in {WEAK_IDENTITY, NO_IDENTITY} and equal and desc and len(desc) >= 20:
        identity = PERMITTED_EQUAL
        grade = "B"
        reasons.append("equal_with_description")

    if identity in {EXACT_MPN, EXACT_CATALOG_NUMBER, EXACT_MODEL} and equal and grade == "C":
        grade = "B"
        reasons.append("equal_boost")

    # Commercial search key
    if identity in {EXACT_MPN, EXACT_MODEL, EXACT_CATALOG_NUMBER, EXACT_NSN, EXACT_UPC, PERMITTED_EQUAL}:
        bits = [mfr, pn or cat or model or nsn or upc]
        if desc and not (pn or cat or model):
            bits.append(desc[:80])
        key = " ".join(str(b) for b in bits if b)
    elif identity == STRONG_GENERIC_SPEC:
        key = desc[:160]
    else:
        key = None

    rec = empty_identity_record(
        manufacturer=mfr,
        manufacturer_raw=norm.get("manufacturer_raw"),
        brand=norm.get("brand"),
        model=model,
        model_raw=norm.get("model_raw"),
        part_number=pn,
        part_number_raw=norm.get("part_number_raw"),
        catalog_number=cat,
        sku=norm.get("sku"),
        nsn=nsn,
        upc=upc,
        raw_description=desc or None,
        quantity=norm.get("quantity"),
        uom=norm.get("uom"),
        uom_normalized=norm.get("uom_normalized"),
        pack_size=norm.get("pack_size"),
        pack_note=norm.get("pack_note"),
        size=norm.get("size"),
        dimensions=norm.get("dimensions"),
        material=norm.get("material"),
        color=norm.get("color"),
        identity_type=identity,
        confidence_grade=grade,
        equal_allowed=equal,
        reference_product=extra.get("reference_product") or ((f"{mfr or ''} {model or pn or ''}".strip()) if equal else None),
        reference_manufacturer=mfr if equal else extra.get("reference_manufacturer"),
        reference_model=(model or pn) if equal else extra.get("reference_model"),
        commercial_search_key=key,
        inherited_manufacturer=bool(norm.get("inherited_manufacturer")),
        enriched_from_spec=bool(extra.get("enriched_from_spec")),
        brand_or_equal_context=bool(extra.get("brand_or_equal_context") or equal),
        confidence_reasons=reasons,
        attributes={
            k: v
            for k, v in {
                "item": norm.get("item"),
                "unit_price": norm.get("unit_price"),
                "model_from_desc_token": extra.get("model_from_desc_token"),
            }.items()
            if v not in (None, "", False)
        },
    )
    return rec
