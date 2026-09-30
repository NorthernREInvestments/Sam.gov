"""Run Phase L.2.6 progressive funnel on live access-YES population."""

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
    parser.add_argument("--deep-limit", type=int, default=40)
    parser.add_argument("--market-max", type=int, default=60)
    parser.add_argument("--usaspending-max", type=int, default=40)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l26_rescue import run_phase_l26_progressive_funnel

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.get("rows") or [])
    print(f"[l26] input rows={len(rows)}", flush=True)

    result = run_phase_l26_progressive_funnel(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        deep_limit=args.deep_limit,
    )
    print(json.dumps({
        "verdict": result.get("verdict"),
        "before_after": result.get("before_after"),
        "triage_counts": result.get("triage_counts"),
        "deep_priority_counts": result.get("deep_priority_counts"),
        "queue_counts": result.get("queue_counts"),
        "cost_by_stage_units": result.get("cost_by_stage_units"),
        "budget_used": result.get("budget_used"),
        "rescued_sample_n": len(result.get("rescued_to_stage3_sample") or []),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
