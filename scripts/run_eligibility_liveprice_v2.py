"""Run eligibility file mining + live public price recovery v2."""
from __future__ import annotations

import json
import sys

from eligibility_and_recovery.liveprice_v2 import run_eligibility_liveprice_v2


def prog(**kw):
    print(
        f"  {kw.get('pct','?'):>3}% {kw.get('phase','')} "
        f"opp={kw.get('opp')} prices={kw.get('prices')} left={kw.get('live_left')}",
        flush=True,
    )


def main() -> int:
    max_opp = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    max_live = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    resume = "--fresh" not in sys.argv
    r = run_eligibility_liveprice_v2(
        on_progress=prog,
        max_opportunities=max_opp,
        max_live_prices=max_live,
        per_opp_price_cap=6,
        resume=resume,
    )
    keys = [
        "ELIGIBILITY_FILE_COVERAGE",
        "TOP_BID_BLOCKERS",
        "TOP_ACTION_REQUIRED",
        "FAR_DFARS",
        "PRICE_ROUTE_HEALTH",
        "LIVE_PRICE_RECOVERY",
        "DISTINCT_OPPORTUNITY_PRICING",
        "HISTORY",
        "BOTH_SIDES",
        "ECONOMICS",
        "PROFIT_BUCKETS",
        "LENDER_PIPELINE",
        "CONSERVATION",
        "MOST_IMPORTANT_ANSWERS",
        "build",
        "run_id",
    ]
    print(json.dumps({k: r.get(k) for k in keys}, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
