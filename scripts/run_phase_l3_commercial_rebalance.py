"""Run Phase L.3 commercial acquisition lane rebalance (fresh hunt + all Stage 3)."""

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
    parser.add_argument("--no-refresh-hunt", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--max-hunt-sources", type=int, default=40)
    parser.add_argument("--max-shell-fetches", type=int, default=3)
    parser.add_argument("--max-detail-fetches", type=int, default=3)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l3_rescue import run_phase_l3_commercial_rebalance

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    result = run_phase_l3_commercial_rebalance(
        authorize_live=not args.no_live,
        refresh_hunt=not args.no_refresh_hunt and not args.no_live,
        max_hunt_sources=args.max_hunt_sources,
        max_shell_fetches=args.max_shell_fetches,
        max_detail_fetches=args.max_detail_fetches,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "fresh_discovery": result.get("fresh_discovery"),
                "acquisition_lanes": result.get("acquisition_lanes"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "acquisition_evidence": result.get("acquisition_evidence"),
                "reverse_economics": result.get("reverse_economics"),
                "profit": result.get("profit"),
                "original_solicitation_integrity": result.get("original_solicitation_integrity"),
                "specialty_pipeline_n": (result.get("specialty_pipeline") or {}).get("count"),
                "quote_required_n": len(result.get("quote_required_queue") or []),
                "deep_escalations_n": len(result.get("deep_escalations") or []),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
