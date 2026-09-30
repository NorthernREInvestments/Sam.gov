"""Run Phase L.7 quote outreach readiness."""

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
    parser.add_argument("--refresh-hunt", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--max-hunt-sources", type=int, default=70)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l7_rescue import run_phase_l7_quote_readiness

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    result = run_phase_l7_quote_readiness(
        authorize_live=not args.no_live,
        refresh_hunt=bool(args.refresh_hunt) and not args.no_live,
        max_hunt_sources=args.max_hunt_sources,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "acquisition_lanes": result.get("acquisition_lanes"),
                "unknown_conversion": result.get("unknown_conversion"),
                "l6_positive_revalidation": result.get("l6_positive_revalidation"),
                "expansion": result.get("expansion"),
                "quote_readiness": result.get("quote_readiness"),
                "ready_profit_tiers": result.get("ready_profit_tiers"),
                "supplier_coverage": result.get("supplier_coverage"),
                "original_solicitation_integrity": result.get("original_solicitation_integrity"),
                "remaining_bottleneck": result.get("remaining_bottleneck"),
                "send_authorized": result.get("send_authorized"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
