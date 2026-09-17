"""Product discovery taxonomy for server-side search / category filters."""

from __future__ import annotations

from typing import Any

PRODUCT_TAXONOMY: dict[str, list[str]] = {
    "IT_HARDWARE": ["IT hardware", "computers", "servers", "laptops", "workstations"],
    "NETWORK": ["network equipment", "switches", "routers", "telecommunications hardware"],
    "ELECTRONICS": ["electronics", "monitors", "peripherals"],
    "VEHICLES": ["vehicles", "trucks", "trailers", "fleet"],
    "HEAVY_EQUIPMENT": ["heavy equipment", "construction equipment", "industrial machinery"],
    "POWER": ["generators", "pumps", "UPS"],
    "HVAC": ["HVAC equipment", "air conditioning equipment"],
    "ELECTRICAL": ["electrical equipment", "transformers"],
    "PLUMBING": ["plumbing equipment"],
    "TOOLS": ["tools", "shop equipment"],
    "SAFETY": ["safety equipment", "PPE"],
    "LAB": ["laboratory equipment", "scientific instruments"],
    "MEDICAL": ["medical equipment", "medical supplies"],
    "JANITORIAL_SUPPLIES": ["janitorial supplies", "maintenance supplies"],
    "FURNITURE": ["furniture", "office equipment", "appliances"],
    "COMMODITIES": ["food commodities", "feed"],
    "WATER": ["water equipment", "wastewater equipment"],
    "AIRPORT": ["airport equipment"],
    "TRANSIT": ["transit equipment"],
    "PUBLIC_WORKS": ["public works equipment", "parts", "materials", "supplies"],
}


def taxonomy_search_terms(*, categories: list[str] | None = None, limit: int = 40) -> list[str]:
    cats = categories or list(PRODUCT_TAXONOMY.keys())
    terms: list[str] = []
    for c in cats:
        for t in PRODUCT_TAXONOMY.get(c, []):
            if t not in terms:
                terms.append(t)
            if len(terms) >= limit:
                return terms
    return terms


def taxonomy_report() -> dict[str, Any]:
    return {
        "categories": {k: list(v) for k, v in PRODUCT_TAXONOMY.items()},
        "category_count": len(PRODUCT_TAXONOMY),
        "note": "Keyword search is not the only discovery method — prefer full listing + local classify when cheap",
        "LIVE_API_REQUESTS": 0,
    }
