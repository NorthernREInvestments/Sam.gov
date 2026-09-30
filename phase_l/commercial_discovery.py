"""Phase L.3 — commercial-product-first discovery (parallel to broad hunt)."""

from __future__ import annotations

import re
from typing import Any

from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    MILSPEC_OPEN_CHANNEL,
    QUOTE_REQUIRED_COMMERCIAL,
    classify_acquisition_lane,
    commercial_acquisition_score,
)

# Category keyword pools for COMMERCIAL_PRODUCT_HUNT tagging
COMMERCIAL_CATEGORY_POOLS: dict[str, tuple[str, ...]] = {
    "IT": (
        "laptop", "desktop", "server", "monitor", "printer", "scanner", "UPS",
        "network", "storage", "display", "PowerEdge", "Latitude", "ThinkPad",
    ),
    "TOOLS": (
        "drill", "saw", "grinder", "torque", "welder", "compressor", "generator",
        "diagnostic", "Milwaukee", "DeWalt", "Makita",
    ),
    "MRO": (
        "pump", "pumps", "motor", "motors", "bearing", "valve", "valves", "fitting", "hose", "filter",
        "electrical", "HVAC", "facility repair",
    ),
    "EQUIPMENT": (
        "skid steer", "tractor", "utility vehicle", "mower", "forklift",
        "trailer", "ToolCat", "Bobcat", "Kubota", "John Deere", "excavator",
        "crane", "loader",
    ),
    "FLEET": (
        "pickup", "SUV", "van", "police", "fleet", "vehicle", "vehicles",
        "F-150", "Expedition", "Police Responder", "SSV", "PPV", "shuttle",
    ),
    "SAFETY": ("helmet", "eyewear", "fall protection", "PPE", "fire extinguisher"),
    "OFFICE": ("furniture", "shelving", "appliance", "storage cabinet", "workstation", "workstations"),
    "LAB": ("multimeter", "oscilloscope", "analyzer", "Fluke", "Keysight", "calibration"),
}

COMMERCIAL_BRANDS = (
    "Dell", "HP", "Lenovo", "Cisco", "Canon", "Epson", "Brother", "Samsung", "LG",
    "Ford", "Chevrolet", "Ram", "Bobcat", "John Deere", "Kubota", "Caterpillar",
    "Milwaukee", "DeWalt", "Makita", "Fluke", "Keysight", "Tektronix", "RIDGID",
)


def commercial_product_hunt_tags(row: dict[str, Any]) -> dict[str, Any]:
    """Tag a row for COMMERCIAL_PRODUCT_HUNT without replacing broad discovery."""
    blob = " ".join(str(row.get(k) or "") for k in ("title", "description", "agency", "solicitation_id"))
    cats = []
    for cat, kws in COMMERCIAL_CATEGORY_POOLS.items():
        if any(re.search(rf"\b{re.escape(k)}\b", blob, re.I) for k in kws):
            cats.append(cat)
    brands = [b for b in COMMERCIAL_BRANDS if re.search(rf"\b{re.escape(b)}\b", blob, re.I)]
    hit = bool(cats or brands)
    return {
        "commercial_product_hunt": hit,
        "commercial_categories": cats,
        "commercial_brands": brands,
        "queue": "COMMERCIAL_PRODUCT_HUNT" if hit else None,
    }


def annotate_rows_with_lanes(
    rows: list[dict[str, Any]],
    *,
    family_memory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Attach acquisition lane + commercial hunt tags to each row (in place copy)."""
    out = []
    for row in rows:
        r = dict(row)
        tags = commercial_product_hunt_tags(r)
        lane = classify_acquisition_lane(r, family_memory=family_memory)
        cas = lane.get("commercial_acquisition_score") or commercial_acquisition_score(r)
        r["acquisition_lane"] = lane["acquisition_lane"]
        r["lane_priority"] = lane["lane_priority"]
        r["commercial_acquisition_score"] = cas.get("score")
        r["commercial_acquisition_band"] = cas.get("band")
        r["lane_classification"] = lane
        r["commercial_product_hunt"] = tags["commercial_product_hunt"]
        r["commercial_categories"] = tags["commercial_categories"]
        r["commercial_brands"] = tags["commercial_brands"]
        # Boost research priority for commercial hunt hits
        if tags["commercial_product_hunt"] and lane["acquisition_lane"] in {
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        }:
            r["primary_commercial_priority"] = True
        else:
            r["primary_commercial_priority"] = bool(lane.get("primary_research_priority"))
        out.append(r)
    return out


def lane_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    c: Counter = Counter()
    for r in rows:
        c[str(r.get("acquisition_lane") or "UNKNOWN")] += 1
    return dict(c)


def commercial_share(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dist = lane_distribution(rows)
    commercial = sum(
        dist.get(k, 0)
        for k in (
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        )
    )
    specialty = sum(
        dist.get(k, 0)
        for k in (
            "MILSPEC_SPECIALTY",
            "SOURCE_APPROVAL_REQUIRED",
            "SOLE_SOURCE_RESTRICTED",
        )
    )
    total = max(1, len(rows))
    return {
        "total": len(rows),
        "commercial_lanes": commercial,
        "specialty_lanes": specialty,
        "commercial_share_pct": round(100.0 * commercial / total, 1),
        "specialty_share_pct": round(100.0 * specialty / total, 1),
        "distribution": dist,
    }
