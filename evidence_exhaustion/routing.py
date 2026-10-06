"""Product routing — determines research strategy per line.

Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

import re
from typing import Any

from evidence_exhaustion.models import (
    BRAND_OR_EQUAL,
    COMMON_BROAD_CHANNEL,
    SPECIALTY_OEM_NARROW_CHANNEL,
    STRONG_GENERIC_SPEC,
    UNKNOWN_CHANNEL,
)

_OR_EQUAL = re.compile(
    r"\b(?:brand\s+or\s+equal|or\s+approved\s+equal|or\s+equivalent|or\s+equal|"
    r"basis\s+of\s+design|acceptable\s+manufacturers?)\b",
    re.I,
)
_OEM_SPECIALTY = re.compile(
    r"\b(?:OEM|authorized\s+(?:dealer|distributor)|source\s+approval|"
    r"QPL|approved\s+source|proprietary|sole\s+source|NSN|CAGE)\b",
    re.I,
)
_BROAD = re.compile(
    r"\b(?:grainger|zoro|fastenal|office|toner|paper|glove|filter|tape|"
    r"bulb|lamp|led|ppe|safety|mro|industrial|fastener|hose|fitting)\b",
    re.I,
)
_SPEC_CUES = re.compile(
    r"\b(?:\d+\s*(?:ft|in|inch|lb|v|volt|w|watt|gal|mm)|type\s+[a-z0-9]+|"
    r"ansi|astm|ul\s*listed|nema|class\s+[123])\b",
    re.I,
)


def classify_product_routing(identity: dict[str, Any]) -> dict[str, Any]:
    """Return routing class + strategy hints. Never reject specialty."""
    desc = str(identity.get("raw_description") or identity.get("description") or "")
    mfr = str(identity.get("manufacturer") or identity.get("brand") or "")
    pn = str(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("sku")
        or identity.get("model")
        or ""
    ).strip()
    blob = f"{mfr} {pn} {desc}"
    equal = bool(identity.get("brand_or_equal_context") or identity.get("equal_allowed")) or bool(
        _OR_EQUAL.search(blob)
    )
    has_exact = len(pn) >= 4 and any(ch.isdigit() for ch in pn)
    spec = identity.get("_spec_resolution") or {}
    strong_generic = (
        str(spec.get("class") or "").startswith("STRONG_GENERIC")
        or (not has_exact and bool(_SPEC_CUES.search(desc)) and len(desc) >= 24)
    )

    if equal:
        routing = BRAND_OR_EQUAL
    elif _OEM_SPECIALTY.search(blob) or (
        has_exact and mfr and not _BROAD.search(blob) and len(pn) >= 6
    ):
        # Exact OEM-style PN without broad MRO cues → specialty until proven otherwise
        if _BROAD.search(blob):
            routing = COMMON_BROAD_CHANNEL
        else:
            routing = SPECIALTY_OEM_NARROW_CHANNEL
    elif has_exact and (_BROAD.search(blob) or not mfr):
        routing = COMMON_BROAD_CHANNEL
    elif strong_generic or (not has_exact and _SPEC_CUES.search(desc)):
        routing = STRONG_GENERIC_SPEC
    elif has_exact:
        routing = COMMON_BROAD_CHANNEL
    else:
        routing = UNKNOWN_CHANNEL

    return {
        "routing_class": routing,
        "has_exact_id": has_exact,
        "equal_allowed": equal,
        "strategy": {
            COMMON_BROAD_CHANNEL: "exhaust_broad_then_best_price",
            SPECIALTY_OEM_NARROW_CHANNEL: "channel_intel_then_quote_if_exhausted",
            STRONG_GENERIC_SPEC: "mandatory_attr_product_match",
            BRAND_OR_EQUAL: "reference_then_compliant_equal",
            UNKNOWN_CHANNEL: "probe_then_reclassify",
        }.get(routing, "probe"),
        "do_not_reject_specialty": True,
    }
