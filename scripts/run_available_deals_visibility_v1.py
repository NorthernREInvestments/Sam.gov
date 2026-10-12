"""Rebuild Available Deals visibility buckets from the live opportunity store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _apply_two_sol_patch() -> dict:
    """Additive merge of the two baseline SAM notices if patch file is present."""
    from m3_data_root import data_path
    from phase_l.l23_full_population_funnel import load_store, save_store

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_adv_v1_two_sols_l23_patch.json"))
    if not path.exists():
        return {"applied": False, "reason": "no_patch"}
    blob = json.loads(path.read_text(encoding="utf-8"))
    opps = blob.get("opportunities") or {}
    if not opps:
        return {"applied": False, "reason": "empty"}
    store = load_store()
    added = updated = 0
    for oid, rec in opps.items():
        if oid in store:
            # Prefer richer set-aside / product class from patch
            for k in (
                "set_aside",
                "typeOfSetAside",
                "typeOfSetAsideDescription",
                "sam_raw",
                "product_service_classification",
                "deadline",
                "title",
                "buyer",
                "authoritative_url",
                "solicitation_event_id",
                "row_ref",
            ):
                if rec.get(k) is not None:
                    store[oid][k] = rec.get(k)
            updated += 1
        else:
            store[oid] = rec
            added += 1
    save_store(store)
    return {"applied": True, "added": added, "updated": updated}


def main() -> int:
    from buyer_intelligence.available_deals_visibility_v1 import (
        baseline_lookup_solicitations,
        run_visibility_rebuild,
    )

    patch_info = _apply_two_sol_patch()
    baseline = baseline_lookup_solicitations(
        ["N00104-27-Q-BA25", "SPE4A7-27-T-0317"]
    )
    report = run_visibility_rebuild()
    print(
        json.dumps(
            {
                "two_sol_patch": patch_info,
                "baseline": {
                    k: {"discovered": v.get("discovered"), "hits": v.get("hit_count")}
                    for k, v in (baseline.get("results") or {}).items()
                },
                "counts": report.get("counts"),
                "funnel_after": {
                    k: (report.get("funnel") or {}).get(k)
                    for k in (
                        "total_live_records",
                        "worth_researching",
                        "ready_to_bid",
                        "blocked_ineligible",
                        "commercial_feed_deals_before",
                        "commercial_feed_deals_after",
                        "previously_hidden_missing_pricing_proxy",
                    )
                },
                "top5": [
                    {
                        "product": d.get("product"),
                        "buyer": d.get("buyer"),
                        "deadline": d.get("deadline"),
                        "next": d.get("next_action"),
                        "score": d.get("research_priority_score"),
                    }
                    for d in (report.get("top_worth_researching") or [])[:5]
                ],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
