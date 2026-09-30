"""Run Phase L.2.9 maximum source coverage on entire Stage 3 population."""

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
    parser.add_argument("--max-shell-fetches", type=int, default=3)
    parser.add_argument("--max-detail-fetches", type=int, default=3)
    parser.add_argument("--usaspending-max", type=int, default=50)
    parser.add_argument("--no-parallel", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l29_rescue import run_phase_l29_maximum_source_coverage

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.get("rows") or [])
    print(f"[l29] input rows={len(rows)}", flush=True)

    result = run_phase_l29_maximum_source_coverage(
        rows,
        authorize_live=not args.no_live,
        max_shell_fetches=args.max_shell_fetches,
        max_detail_fetches=args.max_detail_fetches,
        usaspending_max=args.usaspending_max,
        parallel_branches=not args.no_parallel,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "opportunity_coverage": result.get("opportunity_coverage"),
                "funnel": result.get("funnel"),
                "source_by_source_top": (result.get("source_by_source") or [])[:12],
                "recurring_buy": result.get("recurring_buy"),
                "manual_fallback_n": len(result.get("manual_price_verification_priority") or []),
                "deep_escalations_n": len(result.get("deep_escalations") or []),
                "failure_taxonomy": (result.get("failure_taxonomy") or [])[:8],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
