"""Run material acquisition gap closure on seed BOM."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3_discovery_service import restore_pipeline_store_from_db
from m3_material_acquisition_gap import analyze_material_acquisition_gaps
from m3_pipeline_store import M3PipelineStore

store = M3PipelineStore()
restore_pipeline_store_from_db(store)
run = analyze_material_acquisition_gaps(store, persist=True, max_gaps=10, max_pages_per_gap=6)
print("ok", run.get("ok"))
print("INVENTORY", run.get("INVENTORY"))
print("RECON", json.dumps(run.get("RECONCILIATION"), indent=2, default=str)[:1500])
print("BASELINE", run.get("BASELINE"))
print("AFTER", run.get("AFTER"))
print("PRAIRIE", json.dumps(run.get("PRAIRIE_DROPSEED"), indent=2, default=str)[:1500])
print("RESEARCHED", json.dumps(run.get("MATERIAL_GAPS_RESEARCHED"), indent=2, default=str)[:4000])
b = run.get("BASKET_ECONOMICS") or {}
print("BASKET", {k: b.get(k) for k in (
    "label","MODELED_BASKET_REVENUE","MODELED_BASKET_ACQUISITION_COST","MODELED_GROSS_PRODUCT_SPREAD",
    "Acquisition_quantity_coverage_pct","Primary_L1_L3_quantity_coverage_pct","COMMERCIAL_VERIFICATION","Freight","Financing"
)})
print("BOUNDS", run.get("BASKET_ECONOMIC_BOUNDS"))
print("HEADROOM", run.get("TRANSACTION_10K_TARGET"))
print("WHOLESALE", json.dumps((run.get("WHOLESALE_TARGETS") or [])[:6], indent=2, default=str)[:2000])
print("SUPPLIERS", json.dumps(run.get("SUPPLIER_CONSOLIDATION") or [], indent=2, default=str)[:2000])
print("STOP", run.get("RESEARCH_STOP"))
print("REMAINING", json.dumps([{k:g.get(k) for k in ('Species','Scientific_name','Total_required_quantity','Potential_government_revenue')} for g in (run.get('REMAINING_MATERIAL_GAPS') or [])[:8]], indent=2))
print("COST", {k:v for k,v in (run.get("COST") or {}).items() if k!="notes"})
print("ACCESS", run.get("ACCESS"))
print("SAFETY", run.get("SAFETY"))
print("NEXT", run.get("NEXT_STATE"))
print("VERIFY_GEN", (run.get("COMMERCIAL_VERIFICATION_PACKAGE") or {}).get("Generated"))
