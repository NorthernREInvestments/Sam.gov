"""Run Phase L.4 commercial feed expansion (fresh hunt + all Stage 3)."""

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
    parser.add_argument("--max-hunt-sources", type=int, default=110)
    parser.add_argument("--max-shell-fetches", type=int, default=3)
    parser.add_argument("--max-detail-fetches", type=int, default=3)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l4_rescue import run_phase_l4_commercial_feed_expansion

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    result = run_phase_l4_commercial_feed_expansion(
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
                "fresh_discovery": {
                    "accessible": (result.get("fresh_discovery") or {}).get("accessible"),
                    "discovery_meta": (result.get("fresh_discovery") or {}).get("discovery_meta"),
                },
                "source_family_contribution": result.get("source_family_contribution"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "acquisition_lanes": result.get("acquisition_lanes"),
                "commercial_improvement": result.get("commercial_improvement"),
                "acquisition_evidence": result.get("acquisition_evidence"),
                "reverse_economics": result.get("reverse_economics"),
                "profit": result.get("profit"),
                "original_solicitation_integrity": result.get("original_solicitation_integrity"),
                "remaining_bottleneck": result.get("remaining_bottleneck"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
