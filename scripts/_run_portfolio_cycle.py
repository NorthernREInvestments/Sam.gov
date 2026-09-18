"""Run bounded real portfolio deal analysis cycle."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3_discovery_service import restore_pipeline_store_from_db
from m3_pipeline_store import M3PipelineStore
from m3_portfolio_deal_analysis import (
    inventory_portfolio,
    run_portfolio_cycle,
    scale_fixture_portfolio_test,
)

store = M3PipelineStore()
restore_pipeline_store_from_db(store)
baseline = inventory_portfolio(store)
print("BASELINE", json.dumps(baseline["counts"], indent=2))
print("TOTAL", baseline["total"])

run = run_portfolio_cycle(
    store,
    persist=True,
    max_promote=12,
    max_per_tier={1: 6, 2: 5, 3: 4, 4: 3, 5: 1},
    include_discovery_tick=False,
    resume=True,
)
print("OK", run.get("ok"))
print("AFTER", json.dumps(run.get("AFTER"), indent=2))
print("SUMMARY_KEYS", {k: (run.get("SUMMARY") or {}).get(k) for k in (
    "Cheap_screen_passed", "Cheap_deferred", "Packages_researched", "Products_identified",
    "Government_price_evidence", "Commercial_acquisition_evidence", "Material_gap_research",
    "Research_promotions", "Commercial_verification_worthy", "First_transaction_candidates",
    "Partial_economics", "Economics_established",
)})
print("COST", run.get("COST"))
print("SAFETY", run.get("SAFETY"))
print("CHECKPOINT", run.get("CHECKPOINT"))
print("NEXT", run.get("NEXT_STATE"))
print("TOP")
for c in (run.get("TOP_DEAL_CARDS") or [])[:6]:
    print("-", c.get("Opportunity"), "|", c.get("Status"), "| gross", c.get("Known_gross_spread"), "| next", c.get("NEXT"))

scale = scale_fixture_portfolio_test(25000)
print("SCALE", {k: scale.get(k) for k in ("ok", "candidates", "promotable", "priority_counts", "checkpoint_resume_remaining")})
