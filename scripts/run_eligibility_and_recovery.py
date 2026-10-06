"""Run eligibility gate + evidence recovery sweep."""
from __future__ import annotations

import json
import sys

from eligibility_and_recovery.sweep import run_eligibility_and_recovery_sweep


def prog(**kw):
    print(
        f"  {kw.get('pct', '?'):>3}% {kw.get('phase', '')} "
        f"att={kw.get('attempted')} both={kw.get('both')} opp={kw.get('opp')}",
        flush=True,
    )


def main() -> int:
    max_id = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    max_opp = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    resume = "--fresh" not in sys.argv
    allow_live = "--live-price" in sys.argv
    r = run_eligibility_and_recovery_sweep(
        on_progress=prog,
        resume=resume,
        max_identities=max_id,
        max_opportunities=max_opp,
        allow_live_price=allow_live,
    )
    keys = [
        "ELIGIBILITY",
        "REPROCESSING",
        "PRICE_RECOVERY",
        "HISTORY_RECOVERY",
        "NO_TOKEN_RECOVERY",
        "DISTINCT_OPPORTUNITY_FUNNEL",
        "ECONOMICS",
        "PROFIT_BUCKETS",
        "LENDER_PIPELINE",
        "CONSERVATION",
        "TOP_OWNER_CANDIDATES",
        "MOST_IMPORTANT_ANSWERS",
        "DIFFERENCE_FROM_INPUT_IDENTITIES",
        "DIFFERENCE_FROM_INPUT_OPPORTUNITIES",
        "build",
        "run_id",
    ]
    out = {k: r.get(k) for k in keys}
    print(json.dumps(out, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
