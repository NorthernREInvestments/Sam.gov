"""Run Phase L.5 commercial retention repair."""

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
    parser.add_argument("--no-stage3", action="store_true", help="Audit/counterfactual only")
    parser.add_argument("--max-hunt-sources", type=int, default=90)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l5_rescue import run_phase_l5_commercial_retention_repair

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    result = run_phase_l5_commercial_retention_repair(
        authorize_live=not args.no_live,
        refresh_hunt=bool(args.refresh_hunt) and not args.no_live,
        max_hunt_sources=args.max_hunt_sources,
        run_stage3_economics=not args.no_stage3,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "l4_attrition_audit": result.get("l4_attrition_audit"),
                "counterfactual_replay": result.get("counterfactual_replay"),
                "recovered_n": (result.get("recovered_commercial_candidates") or {}).get("count"),
                "commercial_false_negatives": result.get("commercial_false_negatives"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "acquisition_lanes": result.get("acquisition_lanes"),
                "acquisition_evidence": result.get("acquisition_evidence"),
                "profit": result.get("profit"),
                "remaining_bottleneck": result.get("remaining_bottleneck"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
