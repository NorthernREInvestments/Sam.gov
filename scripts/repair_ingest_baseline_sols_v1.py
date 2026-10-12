"""After baseline evidence: ingest the two SAM notices found via compact solnum.

Does NOT call SAM again — uses saved baseline raw payloads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from buyer_intelligence.available_deals_visibility_v1 import classify_record, run_visibility_rebuild
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _save, _utc
from phase_l.l23_full_population_funnel import load_store, save_store
from phase_l.sam_supplier_conversion_run import ingest_sam_rows


def main() -> int:
    baseline_path = ROOT / "_og_commercial_research_pipeline_out" / "m3_adv_v1_sam_baseline_lookup.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    sam = baseline.get("sam_api") or {}
    rows = []
    for label in ("N00104-27-Q-BA25", "SPE4A7-27-T-0317"):
        raw = (sam.get(label) or {}).get("raw")
        if not raw:
            print("MISSING_RAW", label)
            continue
        rows.append(raw)
        print("queue", label, raw.get("solicitationNumber"), raw.get("typeOfSetAside"), raw.get("title"))

    store = load_store()
    before = len(store)
    info = ingest_sam_rows(store, rows)
    save_store(store)
    print("ingest", info, "store", before, "->", len(store))

    # Classify both for report
    classified = {}
    for oid in info.get("new_ids") or []:
        rec = store.get(oid) or {}
        row = classify_record(oid, rec)
        classified[oid] = {
            "solicitation": rec.get("solicitation_event_id"),
            "title": rec.get("title"),
            "set_aside": rec.get("set_aside"),
            "typeOfSetAside": rec.get("typeOfSetAside"),
            "visibility_bucket": row.get("visibility_bucket"),
            "eligible": row.get("eligible"),
            "eligibility_bucket": row.get("eligibility_bucket"),
            "product_class": row.get("product_class"),
            "deadline": row.get("deadline"),
            "next_action": row.get("next_action"),
        }
        print(
            "classified",
            classified[oid]["solicitation"],
            classified[oid]["visibility_bucket"],
            classified[oid]["eligible"],
            classified[oid]["eligibility_bucket"],
        )

    report = run_visibility_rebuild()
    _save(
        "_og_commercial_research_pipeline_out/m3_adv_v1_post_repair_two_sols.json",
        {"at": _utc(), "ingest": info, "classified": classified, "visibility_counts": report.get("counts")},
    )
    print("visibility", report.get("counts"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
