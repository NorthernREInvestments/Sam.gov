"""Run Phase L cross-government accessible profit hunt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase L cross-government hunt")
    parser.add_argument("--max-sources", type=int, default=40)
    parser.add_argument("--offline-seeds", type=str, default="", help="Optional JSON list of seed rows")
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.hunt import run_phase_l_hunt

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    seeds = None
    if args.offline_seeds:
        path = Path(args.offline_seeds)
        seeds = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(seeds, dict):
            seeds = seeds.get("rows") or seeds.get("results") or []

    result = run_phase_l_hunt(
        authorize_live=not args.no_live,
        max_sources=args.max_sources,
        seed_rows=seeds,
    )
    summary = {
        "source_mix": result.get("source_mix"),
        "counts": result.get("counts"),
        "primary_blockers": result.get("primary_blockers")[:8],
        "top5": [
            {
                "title": (t.get("title") or "")[:80],
                "source_level": t.get("source_level"),
                "our_bid_access": t.get("our_bid_access"),
                "profit_tier": t.get("profit_tier"),
                "expected_net_profit": t.get("expected_net_profit"),
                "actionable_state": t.get("actionable_state"),
            }
            for t in (result.get("top20") or [])[:5]
        ],
        "discovery_meta": result.get("discovery_meta"),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
