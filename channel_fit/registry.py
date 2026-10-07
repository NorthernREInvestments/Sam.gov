"""Known distributor / channel registry + role classification.

Not exhaustive — pattern detector + owner overrides. Do not invent awards.
"""

from __future__ import annotations

import re
from typing import Any

# Vertical → known national/regional distributors (seed knowledge, expandable).
DISTRIBUTOR_REGISTRY: dict[str, tuple[str, ...]] = {
    "WATERWORKS": (
        "ferguson waterworks",
        "ferguson",
        "pace supply",
        "western nevada supply",
        "sierra mountain pipe",
        "core & main",
        "core and main",
        "hd supply waterworks",
        "fortiline",
        "winwater",
    ),
    "MRO": (
        "grainger",
        "fastenal",
        "msc industrial",
        "msc",
        "motion industries",
        "applied industrial",
        "zoro",
    ),
    "ELECTRICAL": (
        "graybar",
        "wesco",
        "rexel",
        "ced",
        "consolidated electrical",
        "border states electric",
    ),
    "PLUMBING_HVAC": (
        "ferguson",
        "winsupply",
        "johnstone supply",
        "baker distributing",
        "carrier enterprise",
    ),
    "MEDICAL": (
        "mckesson",
        "medline",
        "cardinal health",
        "henry schein",
        "owens & minor",
    ),
    "OFFICE": (
        "staples",
        "office depot",
        "odp business",
        "quill",
    ),
}

VERTICAL_TITLE_HINTS: dict[str, re.Pattern[str]] = {
    "WATERWORKS": re.compile(
        r"\b(waterworks|water\s+materials|pipe\s+supply|hydrant|meter\s+box|ductile|"
        r"pvc\s+water|c900|awwa|valve\s+box|backflow)\b",
        re.I,
    ),
    "MRO": re.compile(r"\b(mro|industrial\s+supply|fastener|bearing|maintenance\s+supply)\b", re.I),
    "ELECTRICAL": re.compile(r"\b(electrical\s+supply|conduit|breaker|panelboard|wire\s+cable)\b", re.I),
    "PLUMBING_HVAC": re.compile(r"\b(plumbing|hvac|furnace|refrigerant|copper\s+pipe)\b", re.I),
    "MEDICAL": re.compile(r"\b(medical\s+supply|surgical|exam\s+glove|syringe)\b", re.I),
    "OFFICE": re.compile(r"\b(office\s+supply|toner|copy\s+paper)\b", re.I),
}

# Companies that are often distributors but may also be OUR_SUPPLIER depending on context.
DUAL_ROLE_NAMES = {
    "grainger",
    "ferguson",
    "fastenal",
    "msc",
    "graybar",
}


def normalize_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9&\s]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def detect_vertical(title: str, category: str | None = None) -> str | None:
    blob = f"{title or ''} {category or ''}"
    for vertical, rx in VERTICAL_TITLE_HINTS.items():
        if rx.search(blob):
            return vertical
    return None


def classify_bidder_type(
    name: str,
    *,
    vertical: str | None = None,
) -> str:
    """Return DISTRIBUTOR | RESELLER | MANUFACTURER | UNKNOWN — evidence-based from registry only."""
    n = normalize_name(name)
    if not n:
        return "UNKNOWN"
    # Manufacturer-ish tokens
    if any(t in n for t in ("manufacturing", "mfg", "industries mfg", "oem")):
        return "MANUFACTURER"
    # Check registry
    for vert, names in DISTRIBUTOR_REGISTRY.items():
        if vertical and vert != vertical and vert not in {"MRO", "PLUMBING_HVAC"}:
            # Still allow match but prefer vertical match later
            pass
        for known in names:
            if known in n or n in known:
                return "DISTRIBUTOR"
    # Generic reseller signals
    if any(t in n for t in ("supply co", "supplies", "trading", "solutions", "services", "enterprise")):
        # Ambiguous — many distributors also use these; leave UNKNOWN unless registry hit
        return "UNKNOWN"
    return "UNKNOWN"


def role_for_company(
    name: str,
    *,
    vertical: str | None,
    channel_class: str | None,
) -> str:
    """OUR_SUPPLIER | LIKELY_COMPETITOR | BOTH | UNKNOWN"""
    n = normalize_name(name)
    bidder_type = classify_bidder_type(name, vertical=vertical)
    if bidder_type != "DISTRIBUTOR":
        return "UNKNOWN"
    # Dual-role companies: competitor in their vertical core, supplier elsewhere
    is_dual = any(d in n for d in DUAL_ROLE_NAMES)
    if channel_class == "D_CHANNEL_DOMINATED" and vertical:
        # In dominated vertical, registry distributors are competitors
        for known in DISTRIBUTOR_REGISTRY.get(vertical, ()):
            if known in n:
                return "LIKELY_COMPETITOR"
    if is_dual and channel_class in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL"}:
        return "OUR_SUPPLIER"
    if is_dual:
        return "BOTH"
    if bidder_type == "DISTRIBUTOR" and vertical:
        for known in DISTRIBUTOR_REGISTRY.get(vertical, ()):
            if known in n:
                return "LIKELY_COMPETITOR"
    return "UNKNOWN"


def owner_override_lookup(opportunity_id: str) -> dict[str, Any] | None:
    """Optional durable owner overrides."""
    try:
        import json
        from m3_data_root import data_path

        path = data_path("m3_channel_fit_overrides_v1.json")
        if not path.exists():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return (doc.get("by_opportunity") or {}).get(str(opportunity_id))
    except Exception:
        return None
