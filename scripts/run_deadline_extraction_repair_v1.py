"""Run deadline extraction repair against the live L23 store, then rebuild Available Deals."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _apply_exported_patch() -> dict:
    """Additive apply of m3_der_v1_deadline_patch.json (production-safe remap by URL)."""
    from buyer_intelligence.deadline_extraction_repair_v1 import (
        OUT,
        _bidnet_sol_from_url,
        _index_store_for_deadline_remap,
        apply_candidate,
    )
    from m3_data_root import data_path
    from phase_l.l23_full_population_funnel import load_store, save_store

    path = Path(data_path(f"{OUT}/m3_der_v1_deadline_patch.json"))
    if not path.exists():
        return {"applied": False, "reason": "no_patch"}
    blob = json.loads(path.read_text(encoding="utf-8"))
    patches = blob.get("patches") or {}
    store = load_store()
    index = _index_store_for_deadline_remap(store)
    n = 0
    for old_oid, patch in patches.items():
        if not isinstance(patch, dict):
            continue
        oid = old_oid if old_oid in store else None
        if not oid:
            sol = _bidnet_sol_from_url(patch.get("authoritative_url"))
            if sol and f"sol:{sol}" in index:
                oid = index[f"sol:{sol}"]
        if not oid or oid not in store:
            continue
        rec = store[oid]
        result = apply_candidate(
            rec,
            deadline=patch.get("deadline"),
            deadline_raw=patch.get("deadline_raw"),
            timezone_name=patch.get("deadline_timezone"),
            tz_confidence=patch.get("deadline_tz_confidence"),
            source="der_v1_deadline_patch",
            evidence_url=patch.get("authoritative_url"),
        )
        if result.get("applied"):
            store[oid] = rec
            n += 1
    if n:
        save_store(store)
    return {"applied": True, "filled": n, "patch_size": len(patches)}


def main() -> int:
    from buyer_intelligence.available_deals_visibility_v1 import run_visibility_rebuild
    from buyer_intelligence.deadline_extraction_repair_v1 import run_deadline_repair

    patch_info = _apply_exported_patch()
    report = run_deadline_repair(apply=True)
    report["exported_patch"] = patch_info
    vis = run_visibility_rebuild()
    out = {
        "deadline_repair": {
            "newly_recovered": report.get("newly_recovered"),
            "newly_confirmed_future_delta": report.get("newly_confirmed_future_delta"),
            "before": report.get("before"),
            "after": report.get("after"),
            "by_source": report.get("by_source"),
        },
        "visibility_counts": vis.get("counts"),
        "top5_open": [
            {
                "product": d.get("product"),
                "deadline": d.get("deadline"),
                "days_left": d.get("days_left"),
                "deadline_status": d.get("deadline_status"),
                "next": d.get("next_action"),
            }
            for d in (vis.get("top_worth_researching") or [])[:5]
        ],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
