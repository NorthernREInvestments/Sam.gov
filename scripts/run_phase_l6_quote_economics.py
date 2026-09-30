"""Run Phase L.6 quote-required economics."""

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
    from phase_l.l6_rescue import run_phase_l6_quote_economics

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    result = run_phase_l6_quote_economics(
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
                "government_side_value": result.get("government_side_value"),
                "max_buy_engine": result.get("max_buy_engine"),
                "supplier_mapping": result.get("supplier_mapping"),
                "quote_economics": result.get("quote_economics"),
                "public_price_economics": result.get("public_price_economics"),
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
