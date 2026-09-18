"""Bounded BidNet-network discovery validation — highest-yield family recovery."""
from __future__ import annotations

import json
import time
from pathlib import Path

from discovery.bidnet_network import all_bidnet_networks_enriched
from discovery.live_runner import run_live_discovery
from national_procurement_coverage_map import (
    build_discovery_gap_queue,
    build_national_procurement_coverage_map,
    build_product_survivor_universe,
    build_source_yield_analytics,
)

OUT = Path("data") / "bidnet_network_discovery_validation.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

cands = []
for n in all_bidnet_networks_enriched():
    cands.append(
        {
            "source_id": n["source_id"],
            "name": n["name"],
            "list_url": n["list_url"],
            "adapter_family": n["adapter_family"],
            "platform_family": n.get("platform_family"),
            "kind": "NETWORK",
            "state_code": n.get("state_code"),
            "coverage_class": "LOCAL_NETWORK",
        }
    )

t0 = time.time()
print(f"START BidNet network discovery n={len(cands)}", flush=True)

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
    fetch_details=False,
    candidates_override=cands,
    on_source_complete=_on_source,
)
elapsed = time.time() - t0
metrics = result.get("metrics") or {}
opps = result.get("opportunities") or []
per = metrics.get("per_source") or {}
cmap = build_national_procurement_coverage_map(per_source=per)
yields = build_source_yield_analytics(per_source=per, opportunities=opps)
survivors = build_product_survivor_universe(opps)
gaps = build_discovery_gap_queue(coverage_map=cmap, yield_rows=yields, per_source=per, limit=10)

# Failure taxonomy sample
fails = []
for sid, m in per.items():
    if not m.get("ok") or int(m.get("raw") or 0) == 0:
        fails.append(
            {
                "source_id": sid,
                "stop": m.get("source_stop_reason") or m.get("pagination_stop_reason"),
                "explicit": m.get("explicit_state"),
                "raw": m.get("raw"),
                "pages": m.get("pages_fetched"),
            }
        )

payload = {
    "elapsed_seconds": round(elapsed, 1),
    "raw_records": metrics.get("raw_records"),
    "unique_records": metrics.get("unique_records"),
    "listing_records": metrics.get("listing_records"),
    "sources_attempted": metrics.get("sources_attempted"),
    "sources_successful": metrics.get("sources_successful"),
    "sources_auth_blocked": metrics.get("sources_auth_blocked"),
    "sources_failed": metrics.get("sources_failed"),
    "pages_fetched_total": metrics.get("pages_fetched_total"),
    "opportunities_returned": len(opps),
    "target_5000_reached": int(metrics.get("unique_records") or len(opps) or 0) >= 5000,
    "product_survivor_universe": survivors,
    "top_sources": [
        {"source_id": y["source_id"], "raw": y["current_records"], "access": y["access_state"]}
        for y in yields
        if int(y.get("current_records") or 0) > 0
    ][:30],
    "failures": fails[:20],
    "discovery_gaps": gaps[:8],
}
OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(json.dumps({k: payload[k] for k in [
    "elapsed_seconds", "raw_records", "unique_records", "listing_records",
    "sources_attempted", "sources_successful", "sources_auth_blocked",
    "opportunities_returned", "target_5000_reached", "pages_fetched_total"
]}, indent=2), flush=True)
print("WROTE", OUT, flush=True)
