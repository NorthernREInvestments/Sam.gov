"""Run Phase L.2.1 market-price rescue on accessible Phase L pool."""

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
    parser.add_argument("--market-max", type=int, default=100)
    parser.add_argument("--usaspending-max", type=int, default=50)
    parser.add_argument("--deep-limit", type=int, default=100)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l21_rescue import run_phase_l21_rescue

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in (data.get("rows") or []) if str(r.get("our_bid_access") or "") == "YES"]
    print(f"[l21] input access_yes={len(rows)}", flush=True)

    result = run_phase_l21_rescue(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        deep_limit=args.deep_limit,
    )
    summary = {
        "counts": result.get("counts"),
        "funnel": result.get("funnel"),
        "budget_used": result.get("budget_used"),
        "price_sources": result.get("price_source_distribution"),
        "failures": (result.get("failure_reasons") or [])[:12],
        "public_hits": len(result.get("public_price_hits") or []),
        "top5": [
            {
                "title": (t.get("title") or "")[:70],
                "expected_net": t.get("expected_net_profit"),
                "tier": t.get("profit_tier"),
                "market": t.get("public_retail_price"),
                "hist": t.get("historical_award_unit_price"),
            }
            for t in (result.get("top20") or [])[:5]
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
