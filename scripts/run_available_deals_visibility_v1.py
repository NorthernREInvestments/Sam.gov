"""Rebuild Available Deals visibility buckets from the live opportunity store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from buyer_intelligence.available_deals_visibility_v1 import (
        baseline_lookup_solicitations,
        run_visibility_rebuild,
    )

    baseline = baseline_lookup_solicitations(
        ["N00104-27-Q-BA25", "SPE4A7-27-T-0317"]
    )
    report = run_visibility_rebuild()
    print(
        json.dumps(
            {
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
