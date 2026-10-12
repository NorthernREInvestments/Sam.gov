"""Run selling-price-first + supplier-advantage + low-friction cohorts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from buyer_intelligence.first_revenue_dual_strategy_v1 import run_dual_strategy

    report = run_dual_strategy(spf_n=25, saf_n=25, low_n=25)
    summary = {
        "A_SPF": report.get("A_SPF"),
        "B_SAF": report.get("B_SAF"),
        "C_LOW": report.get("C_LOW"),
        "rfqs": report.get("rfqs"),
        "finance_packets": report.get("finance_packets"),
        "deals": report.get("deals"),
        "conditional": {
            "spf": (report.get("A_SPF") or {}).get("conditional_positive"),
            "saf": (report.get("B_SAF") or {}).get("conditional_positive"),
        },
        "budget": {
            k: (report.get("budget") or {}).get(k)
            for k in ("openai_spend_today_usd", "sam_calls_today", "paid_research_paused")
        },
        "verified_profitable": report.get("verified_profitable"),
        "execution_ready": report.get("execution_ready"),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
