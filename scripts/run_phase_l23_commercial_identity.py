"""Run Phase L.2.3 commercial identity recovery + expanded market research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-max", type=int, default=120)
    parser.add_argument("--usaspending-max", type=int, default=40)
    parser.add_argument("--deep-limit", type=int, default=55)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l23_rescue import run_phase_l23_rescue

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in (data.get("rows") or []) if str(r.get("our_bid_access") or "") == "YES"]
    print(f"[l23] input access_yes={len(rows)}", flush=True)

    result = run_phase_l23_rescue(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        deep_limit=args.deep_limit,
    )
    summary = {
        "counts": result.get("counts"),
        "funnel": result.get("funnel"),
        "telemetry": result.get("telemetry"),
        "bucket_counts": result.get("bucket_counts"),
        "resolution_stats": result.get("resolution_stats"),
        "budget_used": result.get("budget_used"),
        "bobcat": result.get("bobcat_toolcat"),
        "ford": result.get("ford_police_responder"),
        "verified_hits": len(result.get("public_price_hits") or []),
        "unit_spreads": len(result.get("unit_spread_hits") or []),
        "failures": (result.get("failure_reasons") or [])[:12],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
