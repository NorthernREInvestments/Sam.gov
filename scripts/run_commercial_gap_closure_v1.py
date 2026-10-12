"""Run commercial gap closure — real free/cached acquisition + historical research."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from buyer_intelligence.commercial_gap_closure_v1 import run_gap_closure

    report = run_gap_closure(max_opps=50, price_target=30, per_opp_price_s=55)
    summary = {
        "elapsed_s": report.get("elapsed_s"),
        "scorecard": report.get("A_SCORECARD"),
        "price_failure_classes": report.get("price_failure_classes"),
        "completeness": report.get("completeness"),
        "rfqs": report.get("rfqs"),
        "budget": {
            k: (report.get("budget") or {}).get(k)
            for k in (
                "openai_spend_today_usd",
                "sam_calls_today",
                "paid_research_paused",
            )
        },
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
