"""Technical specification extraction from solicitation / spec documents.

Deterministic regex extraction — never invents missing fields.
Every value carries provenance.
"""

from __future__ import annotations

import re
from typing import Any


def _fact(
    value: Any,
    *,
    confidence: str = "UNKNOWN",
    source_document: str | None = None,
    source_location: str | None = None,
    extraction_method: str = "spec_regex",
    snippet: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence if value not in (None, "", []) else "UNKNOWN",
        "source_document": source_document,
        "source_location": source_location,
        "extraction_method": extraction_method,
        "snippet": (snippet or "")[:240] if snippet else None,
    }


def extract_blade_or_product_specifications(
    text: str,
    *,
    source_document: str | None = None,
) -> dict[str, Any]:
    """Extract snowplow/blade (and general) technical requirements from spec text."""
    t = text or ""
    low = t.lower()
    fields: dict[str, Any] = {}

    def put(key: str, value: Any, *, snippet: str | None = None, location: str | None = None) -> None:
        fields[key] = _fact(
            value,
            confidence="VERIFIED_DOCUMENT" if value not in (None, "", []) else "UNKNOWN",
            source_document=source_document,
            source_location=location,
            snippet=snippet,
        )

    # Dimensions — length / width / thickness
    for key, patterns in {
        "blade_length": [
            r"(?:blade\s+)?length[:\s]+([0-9]+(?:\.[0-9]+)?\s*(?:ft|feet|in|inch|inches|\"|\'))",
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:ft|feet)\s+(?:section|blade|long)",
            r"\b([34])\s*FT\.?\s+SECTION\b",
        ],
        "blade_width": [
            r"(?:blade\s+)?width[:\s]+([0-9]+(?:\.[0-9]+)?\s*(?:in|inch|inches|\"))",
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:in|inch|inches|\")\s*(?:wide|width)",
        ],
        "blade_thickness": [
            r"(?:blade\s+)?thickness[:\s]+([0-9]+(?:\.[0-9]+)?\s*(?:in|inch|inches|\"))",
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:in|inch|inches|\")\s*thick",
        ],
    }.items():
        found = None
        snip = None
        for pat in patterns:
            m = re.search(pat, t, re.I)
            if m:
                found = re.sub(r"\s+", " ", m.group(1 if m.lastindex else 0)).strip()
                snip = m.group(0)
                break
        put(key, found, snippet=snip)

    dims = []
    for k in ("blade_length", "blade_width", "blade_thickness"):
        if fields[k]["value"]:
            dims.append(f"{k}={fields[k]['value']}")
    put("dimensions", "; ".join(dims) if dims else None)

    # Carbide / rubber
    carbide = None
    m = re.search(r"(tungsten[\s\-]?carbide[^\n.]{0,120})", t, re.I)
    if m:
        carbide = re.sub(r"\s+", " ", m.group(1)).strip()
    elif "carbide" in low:
        carbide = "carbide_mentioned"
    put("carbide_configuration", carbide, snippet=m.group(0) if m else None)

    rubber = None
    m = re.search(r"(rubber[\s\-]?encased[^\n.]{0,80}|rubber[^\n.]{0,60})", t, re.I)
    if m and "rubber" in m.group(0).lower():
        rubber = re.sub(r"\s+", " ", m.group(0)).strip()[:160]
    put("rubber_configuration", rubber, snippet=m.group(0) if m else None)

    # Hole pattern / mounting
    hole = None
    m = re.search(r"(hole\s+pattern[^\n.]{0,120}|square\s+holes?[^\n.]{0,80}|bolt\s+pattern[^\n.]{0,120})", t, re.I)
    if m:
        hole = re.sub(r"\s+", " ", m.group(1)).strip()
    put("hole_pattern", hole, snippet=m.group(0) if m else None)

    mount = None
    m = re.search(r"(mount(?:ing)?[^\n.]{0,120})", t, re.I)
    if m:
        mount = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    put("mounting_requirements", mount, snippet=m.group(0) if m else None)

    # Material / hardness
    material = None
    m = re.search(r"(?:material|steel|alloy)[:\s]+([^\n]{5,100})", t, re.I)
    if m:
        material = re.sub(r"\s+", " ", m.group(0)).strip()[:160]
    put("material_requirements", material, snippet=m.group(0) if m else None)

    hardness = None
    m = re.search(r"(?:hardness|Rockwell|Brinell|HRC|HB)[:\s]*([^\n]{2,60})", t, re.I)
    if m:
        hardness = re.sub(r"\s+", " ", m.group(0)).strip()[:120]
    put("hardness", hardness, snippet=m.group(0) if m else None)

    perf = None
    m = re.search(r"(performance[^\n.]{0,160}|must\s+meet[^\n.]{0,120})", t, re.I)
    if m:
        perf = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    put("performance_requirements", perf, snippet=m.group(0) if m else None)

    # Cover strap / back support
    put(
        "cover_strap_requirements",
        "cover_strap_mentioned" if re.search(r"cover\s+strap", low) else None,
    )
    put(
        "back_support_requirements",
        "back_support_mentioned" if re.search(r"back\s+support", low) else None,
    )

    # Brands
    brands = []
    for bm in re.finditer(
        r"(?:approved\s+brands?|manufacturer(?:s)?|brand(?:s)?)[:\s]+([^\n]{5,200})",
        t,
        re.I,
    ):
        brands.append(re.sub(r"\s+", " ", bm.group(1)).strip()[:200])
    put("acceptable_manufacturers_brands", brands or None)

    brand_equal = bool(re.search(r"or\s+equal|comparable\s+brands|not\s+intended\s+to\s+be\s+restrictive", low))
    put("brand_or_equal_language", True if brand_equal else None)

    certs = []
    if re.search(r"mill\s+cert", low):
        certs.append("mill_certification")
    if re.search(r"iso\s*\d+", low):
        certs.append("iso_mentioned")
    put("certifications", certs or None)
    put("mill_certifications", True if "mill_certification" in certs else None)

    put("sample_requirements", True if re.search(r"\bsamples?\b", low) else None)
    put("testing_requirements", True if re.search(r"\btest(?:ing)?\b|\binspection\b", low) else None)

    packaging = None
    m = re.search(r"(pallet[^\n.]{0,160}|packag(?:e|ing)[^\n.]{0,120})", t, re.I)
    if m:
        packaging = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    put("packaging", packaging, snippet=m.group(0) if m else None)

    delivery = None
    m = re.search(r"(F\.?O\.?B\.?[^\n.]{0,120}|deliver(?:y|ies)[^\n.]{0,120})", t, re.I)
    if m:
        delivery = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    put("delivery", delivery, snippet=m.group(0) if m else None)

    warranty = None
    m = re.search(r"(warrant(?:y|ies)[^\n.]{0,120})", t, re.I)
    if m:
        warranty = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    put("warranty", warranty, snippet=m.group(0) if m else None)

    coo = None
    m = re.search(r"(country\s+of\s+origin[^\n.]{0,120}|buy\s+american[^\n.]{0,80}|made\s+in[^\n.]{0,60})", t, re.I)
    if m:
        coo = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    put("country_of_origin_requirements", coo, snippet=m.group(0) if m else None)

    # Other compliance catch-all keywords present
    other = []
    for label, pat in [
        ("tariffs", r"\btariff"),
        ("lead_time", r"lead\s+time"),
        ("authorized_distributor", r"authorized\s+distributor"),
    ]:
        if re.search(pat, low):
            other.append(label)
    put("other_technical_compliance", other or None)

    known = [k for k, v in fields.items() if v.get("value") not in (None, "", [], False)]
    unknown = [k for k, v in fields.items() if v.get("value") in (None, "", [])]

    return {
        "specifications": fields,
        "known_fields": known,
        "unknown_fields": unknown,
        "source_document": source_document,
        "sufficient_for_quote_packet": bool(
            fields.get("carbide_configuration", {}).get("value")
            or fields.get("dimensions", {}).get("value")
            or fields.get("blade_length", {}).get("value")
        ),
    }
