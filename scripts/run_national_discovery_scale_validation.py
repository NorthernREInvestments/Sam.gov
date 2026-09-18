"""ONE national discovery validation cycle — measure real unique current volume."""
from __future__ import annotations

import json
import time
from pathlib import Path

from discovery.live_runner import run_live_discovery
from national_procurement_coverage_map import (
    build_discovery_coverage_health,
    build_discovery_gap_queue,
    build_national_procurement_coverage_map,
    build_product_survivor_universe,
    build_source_yield_analytics,
)

OUT = Path("data") / "national_discovery_scale_validation.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

t0 = time.time()
print("START national discovery validation", flush=True)

def _on_source(metrics: dict, sid: str) -> None:
    print(
        f"SRC {sid} attempted={metrics.get('sources_attempted')} "
        f"ok={metrics.get('sources_successful')} raw={metrics.get('raw_records')} "
        f"unique={metrics.get('unique_records')} pages={metrics.get('pages_fetched_total')}",
        flush=True,
    )

result = run_live_discovery(
    profile="national",
    authorize_live=True,
    persist=False,
    preview=True,
    fetch_details=False,  # volume discovery — cheap metadata only
    on_source_complete=_on_source,
)
elapsed = time.time() - t0
metrics = result.get("metrics") or {}
opps = result.get("opportunities") or result.get("collected") or []
if not opps and isinstance(result.get("preview"), list):
    opps = result["preview"]

per = metrics.get("per_source") or {}
cmap = build_national_procurement_coverage_map(per_source=per)
yields = build_source_yield_analytics(per_source=per, opportunities=opps)
survivors = build_product_survivor_universe(opps)
gaps = build_discovery_gap_queue(coverage_map=cmap, yield_rows=yields, per_source=per, limit=15)
health = build_discovery_coverage_health(coverage_map=cmap, yield_rows=yields, funnel=survivors)

# Family yield rollup
family = {}
for y in yields:
    fam = str(y.get("portal_family") or "UNKNOWN")
    bucket = family.setdefault(fam, {"sources": 0, "productive": 0, "records": 0})
    bucket["sources"] += 1
    if int(y.get("current_records") or 0) > 0:
        bucket["productive"] += 1
        bucket["records"] += int(y.get("current_records") or 0)

payload = {
    "elapsed_seconds": round(elapsed, 1),
    "raw_records": metrics.get("raw_records"),
    "unique_records": metrics.get("unique_records"),
    "listing_records": metrics.get("listing_records"),
    "sources_attempted": metrics.get("sources_attempted"),
    "sources_successful": metrics.get("sources_successful"),
    "sources_failed": metrics.get("sources_failed"),
    "sources_pagination_incomplete": metrics.get("sources_pagination_incomplete"),
    "registered_sources": metrics.get("registered_sources"),
    "eligible_sources": metrics.get("eligible_sources"),
    "partial_reason": result.get("partial_reason") or metrics.get("partial_reason"),
    "stop_run_partial": result.get("stop_run_partial") or metrics.get("stop_run_partial"),
    "opportunities_returned": len(opps),
    "family_yield": dict(sorted(family.items(), key=lambda kv: -kv[1]["records"])),
    "coverage_map_geo": (cmap.get("geographic") or {}).get("states"),
    "coverage_health": health,
    "product_survivor_universe": survivors,
    "discovery_gaps": gaps[:10],
    "top_sources": [
        {"source_id": y["source_id"], "family": y["portal_family"], "raw": y["current_records"], "access": y["access_state"]}
        for y in yields[:25]
        if int(y.get("current_records") or 0) > 0
    ],
    "target_5000_reached": int(metrics.get("unique_records") or len(opps) or 0) >= 5000,
}
OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(json.dumps({k: payload[k] for k in [
    "elapsed_seconds", "raw_records", "unique_records", "listing_records",
    "sources_attempted", "sources_successful", "opportunities_returned", "target_5000_reached"
]}, indent=2), flush=True)
print("WROTE", OUT, flush=True)
