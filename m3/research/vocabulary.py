"""Reusable product vocabulary for broad keyword expansion (deterministic first)."""

from __future__ import annotations

from typing import Any

# Cached synonym sets — extend carefully; exact part-number mode must never use these.
PRODUCT_SYNONYMS: dict[str, list[str]] = {
    "screws": ["screw", "machine screw", "cap screw", "self-tapping screw", "wood screw", "sheet metal screw", "fasteners"],
    "valves": ["valve", "ball valve", "gate valve", "check valve", "plumbing valve", "control valve"],
    "equipment": ["equipment", "apparatus", "machinery", "capital equipment"],
    "appliances": ["appliance", "refrigerator", "washer", "dryer", "range", "dishwasher", "commercial laundry"],
    "generators": ["generator", "genset", "backup power", "standby generator"],
    "hardware": ["hardware", "fasteners", "hinges", "locks", "builders hardware"],
    "pumps": ["pump", "centrifugal pump", "sump pump", "water pump", "circulator"],
    "furniture": ["furniture", "desk", "chair", "table", "filing cabinet", "office furniture"],
    "janitorial": ["janitorial", "cleaning supplies", "custodial", "sanitation"],
    "lighting": ["lighting", "led", "luminaire", "fixture", "lamp"],
    "ppe": ["ppe", "personal protective", "gloves", "safety glasses", "respirator"],
}


def expand_keyword(keyword: str, *, mode: str = "broad") -> list[str]:
    k = (keyword or "").strip().lower()
    if not k:
        return []
    if mode in {"exact_phrase", "exact_mpn"}:
        return [k]
    terms = [k]
    for root, syns in PRODUCT_SYNONYMS.items():
        if k == root or k in syns or any(k in s for s in syns):
            terms.extend([root] + syns)
            break
    # de-dupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_search_terms(
    keyword: str,
    *,
    mode: str = "broad",
    exclude: str = "",
) -> dict[str, Any]:
    """Support multiple terms separated by commas or ' AND '; exclusions via exclude field."""
    raw = (keyword or "").strip()
    parts: list[str] = []
    if " AND " in raw.upper():
        # simple split on AND (case-insensitive)
        import re

        parts = [p.strip() for p in re.split(r"\s+AND\s+", raw, flags=re.I) if p.strip()]
    elif "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    elif raw:
        parts = [raw]

    include: list[str] = []
    for p in parts:
        include.extend(expand_keyword(p, mode=mode))
    # unique
    seen: set[str] = set()
    include_u = []
    for t in include:
        if t not in seen:
            seen.add(t)
            include_u.append(t)

    exclude_terms = [e.strip().lower() for e in (exclude or "").split(",") if e.strip()]
    return {
        "mode": mode,
        "raw": raw,
        "include_terms": include_u,
        "exclude_terms": exclude_terms,
        "is_exact_mpn": mode == "exact_mpn",
    }
