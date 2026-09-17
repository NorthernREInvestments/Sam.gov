"""Product-yield intelligence per source/family — guides discovery effort."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from national_discovery_funnel import stage1_ultra_cheap


def compute_product_yield(
    *,
    source_id: str,
    platform_family: str | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    discovered = len(records)
    survivors = []
    for r in records:
        s1 = stage1_ultra_cheap(r)
        if s1.get("survive"):
            survivors.append({**r, "stage1": s1})
    serious = [s for s in survivors if s.get("stage1", {}).get("classification") in {
        "CORE_PRODUCT", "PRODUCT_PLUS_SERVICE", "UNKNOWN", None
    } or s.get("title")]
    return {
        "kind": "ProductYield",
        "source_id": source_id,
        "platform_family": platform_family,
        "records_discovered": discovered,
        "product_cheap_screen_survivors": len(survivors),
        "serious_research_candidates": len(serious),
        "pursuit_worthy_candidates": 0,  # filled downstream after pursuit qualification
        "yield_rate": round(len(survivors) / discovered, 4) if discovered else 0.0,
        "stop_searching_low_yield": False,  # operator/Cost Governor policy only
    }


def aggregate_yield_by_family(yields: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "records_discovered": 0,
        "product_cheap_screen_survivors": 0,
        "sources": 0,
    })
    for y in yields:
        fam = y.get("platform_family") or "UNKNOWN"
        by[fam]["records_discovered"] += int(y.get("records_discovered") or 0)
        by[fam]["product_cheap_screen_survivors"] += int(y.get("product_cheap_screen_survivors") or 0)
        by[fam]["sources"] += 1
    return {
        "kind": "ProductYieldByFamily",
        "families": [
            {
                "platform_family": fam,
                **stats,
                "yield_rate": round(
                    stats["product_cheap_screen_survivors"] / stats["records_discovered"], 4
                )
                if stats["records_discovered"]
                else 0.0,
            }
            for fam, stats in sorted(by.items(), key=lambda x: -x[1]["records_discovered"])
        ],
    }
