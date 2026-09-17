"""Product category yield — deterministic classification of cheap-screen survivors."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from national_discovery_funnel import stage1_ultra_cheap

PRODUCT_CATEGORIES = (
    "IT_COMPUTERS",
    "ELECTRONICS",
    "TOOLS",
    "INDUSTRIAL_EQUIPMENT",
    "VEHICLES_MOBILE_EQUIPMENT",
    "PARTS",
    "BUILDING_MATERIALS",
    "ELECTRICAL",
    "HVAC",
    "PLUMBING",
    "SAFETY_PPE",
    "JANITORIAL_FACILITY_SUPPLIES",
    "OFFICE_FURNITURE",
    "AGRICULTURAL_GROUNDS",
    "MEDICAL_PRODUCTS",
    "FOOD_COMMODITIES",
    "OTHER_TANGIBLE_GOODS",
    "MIXED_GOODS_SERVICES",
    "LIKELY_SERVICE_FALSE_POSITIVE",
    "UNKNOWN",
)

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("IT_COMPUTERS", re.compile(r"\b(laptop|desktop|computer|server|workstation|chromebook|monitor|printer|network\s+switch|router|storage\s+array|tablet)\b", re.I)),
    ("ELECTRONICS", re.compile(r"\b(electronic|radio|radio\s+equipment|av\s+equipment|camera|sensor|display\s+panel)\b", re.I)),
    ("TOOLS", re.compile(r"\b(hand\s+tools?|power\s+tools?|tool\s+set|wrench|drill|saw)\b", re.I)),
    ("VEHICLES_MOBILE_EQUIPMENT", re.compile(r"\b(vehicle|truck|van|bus|ambulance|fire\s+apparatus|trailer|mower|loader|excavator|forklift|tractor)\b", re.I)),
    ("HVAC", re.compile(r"\b(hvac|air\s+condition|furnace|chiller|boiler|heat\s+pump)\b", re.I)),
    ("ELECTRICAL", re.compile(r"\b(electrical\s+(?:supplies|equipment|materials)|wire|cable|transformer|breaker|lighting\s+fixture|generator)\b", re.I)),
    ("PLUMBING", re.compile(r"\b(plumbing|pipe\b|valve|faucet|water\s+heater|backflow)\b", re.I)),
    ("SAFETY_PPE", re.compile(r"\b(ppe|personal\s+protective|safety\s+(?:equipment|gear|vest)|fire\s+extinguisher|hose\b|respirator|hard\s+hat)\b", re.I)),
    ("JANITORIAL_FACILITY_SUPPLIES", re.compile(r"\b(janitorial\s+supplies|cleaning\s+supplies|facility\s+supplies|paper\s+towel|trash\s+liner)\b", re.I)),
    ("OFFICE_FURNITURE", re.compile(r"\b(furniture|office\s+chair|desk|cubicle|filing\s+cabinet)\b", re.I)),
    ("AGRICULTURAL_GROUNDS", re.compile(r"\b(seed|fertilizer|grounds\s+equipment|irrigation|landscape\s+material|turf)\b", re.I)),
    ("MEDICAL_PRODUCTS", re.compile(r"\b(medical\s+(?:supplies|equipment)|pharmaceutical|syringe|ppe\s+medical|hospital\s+bed)\b", re.I)),
    ("FOOD_COMMODITIES", re.compile(r"\b(food\s+(?:product|commodity|supply)|grocer|produce\b|meal\s+component)\b", re.I)),
    ("BUILDING_MATERIALS", re.compile(r"\b(lumber|concrete|asphalt|roofing|building\s+materials?|drywall|steel\s+beam|aggregate)\b", re.I)),
    ("PARTS", re.compile(r"\b(parts?|replacement\s+component|oem\s+part|spare\s+part|filter|hose|blade|belt)\b", re.I)),
    ("INDUSTRIAL_EQUIPMENT", re.compile(r"\b(equipment|pump|motor|compressor|crane|lift|tank|machinery|industrial)\b", re.I)),
]

_SERVICE_FP = re.compile(
    r"\b((?:equipment|vehicle|hvac|janitorial|grounds?)\s+(?:maintenance|repair|service)s?|"
    r"installation\s+services?|professional\s+services?|consulting\s+services?|"
    r"staffing|custodial\s+services?|software\s+development)\b",
    re.I,
)
_MIXED = re.compile(
    r"\b(supply\s+and\s+install|furnish\s+and\s+install|equipment\s+and\s+(?:related\s+)?services|"
    r"purchase\s+and\s+install|goods\s+and\s+services)\b",
    re.I,
)
_PRODUCT_CORE = re.compile(
    r"\b(purchase|procurement|supply|furnish|delivery|commodit|equipment|supplies|materials|hardware|parts?)\b",
    re.I,
)


def classify_product_category(title: str, description: str = "", category: str = "") -> dict[str, Any]:
    blob = f"{title} {description} {category}".strip()
    if not blob:
        return {"category": "UNKNOWN", "confidence": "LOW", "evidence": "empty"}

    if _SERVICE_FP.search(blob) and not _PRODUCT_CORE.search(title):
        return {"category": "LIKELY_SERVICE_FALSE_POSITIVE", "confidence": "HIGH", "evidence": "service_phrase"}

    if _MIXED.search(blob) or (_SERVICE_FP.search(blob) and _PRODUCT_CORE.search(blob)):
        # Still try a goods family for mixed
        for cat, pat in _RULES:
            if pat.search(blob):
                return {"category": "MIXED_GOODS_SERVICES", "confidence": "MEDIUM", "evidence": cat}
        return {"category": "MIXED_GOODS_SERVICES", "confidence": "MEDIUM", "evidence": "mixed_phrase"}

    for cat, pat in _RULES:
        if pat.search(blob):
            return {"category": cat, "confidence": "HIGH", "evidence": pat.pattern[:40]}

    if _PRODUCT_CORE.search(blob):
        return {"category": "OTHER_TANGIBLE_GOODS", "confidence": "MEDIUM", "evidence": "product_core_hint"}

    return {"category": "UNKNOWN", "confidence": "LOW", "evidence": "no_rule_match"}


def build_product_category_yield(records: list[dict[str, Any]]) -> dict[str, Any]:
    """records: dicts with title/description/status and optional stage1 already applied."""
    by_cat: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "records_discovered": 0,
            "unique_opportunities": 0,
            "open_current": 0,
            "cheap_screen_survivors": 0,
            "serious_research_queued": 0,
        }
    )
    survivors_total = 0
    unique_seen: set[str] = set()
    categorized_survivors: list[dict[str, Any]] = []

    for r in records:
        title = str(r.get("title") or "")
        desc = str(r.get("description") or "")
        cat = classify_product_category(title, desc, str(r.get("category") or ""))
        category = cat["category"]
        uid = str(r.get("canonical_id") or r.get("external_id") or r.get("solicitation_number") or title)[:200]
        slot = by_cat[category]
        slot["records_discovered"] += 1
        if uid not in unique_seen:
            unique_seen.add(uid)
            slot["unique_opportunities"] += 1
        status = str(r.get("status") or "OPEN").upper()
        if status in {"OPEN", "CURRENT", ""}:
            slot["open_current"] += 1
        s1 = r.get("stage1") or stage1_ultra_cheap(r)
        if s1.get("survive"):
            slot["cheap_screen_survivors"] += 1
            slot["serious_research_queued"] += 1
            survivors_total += 1
            categorized_survivors.append({**r, "product_category": category, "category_meta": cat, "stage1": s1})

    rows = []
    for category in PRODUCT_CATEGORIES:
        slot = by_cat.get(category) or {
            "records_discovered": 0,
            "unique_opportunities": 0,
            "open_current": 0,
            "cheap_screen_survivors": 0,
            "serious_research_queued": 0,
        }
        pct = round(100.0 * slot["cheap_screen_survivors"] / survivors_total, 2) if survivors_total else 0.0
        rows.append({"category": category, **slot, "pct_of_survivors": pct})

    rows_sorted = sorted(rows, key=lambda x: -x["cheap_screen_survivors"])
    return {
        "kind": "ProductCategoryYield",
        "total_records": len(records),
        "total_survivors": survivors_total,
        "by_category": rows_sorted,
        "largest_categories": [r["category"] for r in rows_sorted if r["cheap_screen_survivors"] > 0][:5],
        "survivors": categorized_survivors,
    }
