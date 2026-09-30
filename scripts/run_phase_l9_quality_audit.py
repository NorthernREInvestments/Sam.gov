"""Run Phase L.9 positive quality audit."""

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
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l9_rescue import run_phase_l9_quality_audit

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    result = run_phase_l9_quality_audit(
        authorize_live=not args.no_live,
        refresh_hunt=bool(args.refresh_hunt) and not args.no_live,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "quality_audit": result.get("quality_audit"),
                "supplier_quality": result.get("supplier_quality"),
                "quantity_config": result.get("quantity_config"),
                "quote_queues": result.get("quote_queues"),
                "validated_profit_tiers": result.get("validated_profit_tiers"),
                "secondary_profit_tiers": result.get("secondary_profit_tiers"),
                "comparison_table": result.get("comparison_table"),
                "source_upgrades": result.get("source_upgrades"),
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
