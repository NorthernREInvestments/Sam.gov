"""SpecBasedCommercialIdentityResolver — recover researchable identities from no-token lines."""

from __future__ import annotations

import re
from typing import Any

from eligibility_and_recovery.models import (
    BRAND_OR_EQUAL,
    NON_COMMERCIAL,
    REFERENCE_PRODUCT,
    RESEARCHABLE_NO_TOKEN,
    STRONG_GENERIC,
    WEAK_GENERIC,
)

_ITEM_NO = re.compile(
    r"(?:item|cat(?:alog)?(?:\s*#| number)?|sku|model|p/?n|part(?:\s*#| number)?)\s*[#:]?\s*([A-Z0-9][A-Z0-9\-]{2,})",
    re.I,
)
_OR_EQUAL = re.compile(
    r"\bor\s+equivalent\b|\bor\s+equal\b|\bapproved\s+equal\b|\bbrand\s+or\s+equal\b|"
    r"\bbasis\s+of\s+design\b|\bmanufacturer\s+or\s+approved\s+equal\b",
    re.I,
)
_DIM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:\"|in(?:ch(?:es)?)?|ft|feet|mm|cm|gal|lb|lbs|watt|volt|v|amp|hp)\b",
    re.I,
)
_TYPE_CLASS = re.compile(
    r"\b(?:type|class|grade|rating)\s+([A-Z0-9\-]+)\b|"
    r"\b(\d+)\s*lb\b|\b(\d+)\s*volt\b|\b(\d+)\s*amp\b",
    re.I,
)
_MFR_LEAD = re.compile(
    r"^([A-Z][A-Za-z0-9&\.\-]+(?:\s+[A-Z][A-Za-z0-9&\.\-]+){0,2})\s*(?:™|®)?\s+",
)
_NON_COMMERCIAL_HINTS = re.compile(
    r"\blabor\b|\bservices?\b|\binstallation\b|\btraining\b|\bconsulting\b|"
    r"\brental\b|\blease\b|\bmaintenance\s+agreement\b",
    re.I,
)


def _attrs_from_description(desc: str) -> dict[str, Any]:
    dims = [m.group(0) for m in _DIM.finditer(desc)]
    types = []
    for m in _TYPE_CLASS.finditer(desc):
        types.append(next(g for g in m.groups() if g))
    return {
        "dimensions": dims[:8],
        "type_class_rating": types[:6],
        "has_or_equal": bool(_OR_EQUAL.search(desc)),
    }


def resolve_spec_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Build a commercial search identity from description/spec when PN/model missing."""
    desc = str(identity.get("raw_description") or identity.get("description") or "").strip()
    mfr = identity.get("manufacturer") or identity.get("brand")
    attrs_in = identity.get("attributes") if isinstance(identity.get("attributes"), dict) else {}

    item_hits = [m.group(1).upper() for m in _ITEM_NO.finditer(desc)]
    if not mfr:
        m = _MFR_LEAD.match(desc)
        if m and len(m.group(1)) >= 3:
            mfr = m.group(1).strip()

    derived_attrs = _attrs_from_description(desc)
    mandatory: list[str] = []
    optional: list[str] = []
    if mfr:
        mandatory.append(f"manufacturer={mfr}")
    if item_hits:
        mandatory.append(f"reference_item={item_hits[0]}")
    for d in derived_attrs.get("dimensions") or []:
        mandatory.append(f"dim={d}")
    for t in derived_attrs.get("type_class_rating") or []:
        mandatory.append(f"type={t}")
    for k, v in attrs_in.items():
        if v and k not in {"item"}:
            optional.append(f"{k}={v}")

    # Classification
    cls = WEAK_GENERIC
    if _NON_COMMERCIAL_HINTS.search(desc) and not item_hits:
        cls = NON_COMMERCIAL
    elif item_hits or (mfr and len(desc) >= 20):
        if derived_attrs.get("has_or_equal") or identity.get("identity_type") in {
            "PERMITTED_EQUAL",
            "BRAND_OR_EQUAL",
        }:
            cls = REFERENCE_PRODUCT if item_hits else BRAND_OR_EQUAL
        elif len(mandatory) >= 3 or (item_hits and mfr):
            cls = STRONG_GENERIC if not item_hits else REFERENCE_PRODUCT
        elif len(mandatory) >= 2 and len(desc) >= 25:
            cls = STRONG_GENERIC
        else:
            cls = WEAK_GENERIC
    elif len(desc) >= 40 and len(derived_attrs.get("dimensions") or []) >= 2:
        cls = STRONG_GENERIC
    elif len(desc) < 15:
        cls = NON_COMMERCIAL

    # Build synthetic tokens for downstream resolvers
    part_number = item_hits[0] if item_hits else None
    model = None
    if not part_number and mfr and derived_attrs.get("dimensions"):
        # synthetic catalog key from mfr + first dim (search aid only)
        model = None

    commercial_key = " ".join(
        x for x in [mfr, part_number, desc[:120]] if x
    ).strip()

    confidence = "A" if cls == REFERENCE_PRODUCT and part_number else (
        "B" if cls in RESEARCHABLE_NO_TOKEN else "C"
    )

    return {
        "no_token_class": cls,
        "researchable": cls in RESEARCHABLE_NO_TOKEN,
        "commercial_search_identity": commercial_key,
        "mandatory_attributes": mandatory,
        "optional_attributes": optional,
        "manufacturer": mfr,
        "reference_product": part_number,
        "part_number": part_number,  # recovered token
        "model": model,
        "catalog_number": part_number,
        "search_confidence": confidence,
        "raw_description": desc,
        "condition_policy_hint": "NEW_ASSUMED",
        "spec_checklist": mandatory,
    }


def enrich_identity_for_research(identity: dict[str, Any]) -> dict[str, Any]:
    """Return identity copy with recovered tokens when possible."""
    out = dict(identity)
    has_token = bool(
        out.get("part_number")
        or out.get("catalog_number")
        or out.get("model")
        or out.get("sku")
        or out.get("nsn")
    )
    if has_token:
        out["_spec_resolution"] = {"no_token_class": None, "researchable": True, "had_token": True}
        return out
    spec = resolve_spec_identity(out)
    out["_spec_resolution"] = spec
    if spec.get("part_number") and not out.get("part_number"):
        out["part_number"] = spec["part_number"]
        out["_recovered_token"] = True
    if spec.get("manufacturer") and not out.get("manufacturer"):
        out["manufacturer"] = spec["manufacturer"]
    if spec.get("catalog_number") and not out.get("catalog_number"):
        out["catalog_number"] = spec["catalog_number"]
    out["commercial_search_key"] = spec.get("commercial_search_identity")
    return out
