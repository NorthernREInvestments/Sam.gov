"""Run Phase L.2.7 resilient acquisition pricing on Stage 3 population."""

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
    parser.add_argument("--max-fetches", type=int, default=6)
    parser.add_argument("--usaspending-max", type=int, default=40)
    parser.add_argument("--bing-fallback", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l27_rescue import run_phase_l27_resilient_pricing

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = list(data.get("rows") or [])
    print(f"[l27] input rows={len(rows)}", flush=True)

    result = run_phase_l27_resilient_pricing(
        rows,
        authorize_live=not args.no_live,
        max_fetches_per_candidate=args.max_fetches,
        usaspending_max=args.usaspending_max,
        allow_bing_fallback=args.bing_fallback,
    )
    print(
        json.dumps(
            {
                "verdict": result.get("verdict"),
                "funnel": result.get("funnel"),
                "failure_taxonomy": (result.get("failure_taxonomy") or [])[:8],
                "circuit_breaker_skipped": (result.get("circuit_breaker") or {}).get("skipped"),
                "cost": result.get("cost"),
                "manual_fallback_n": len(result.get("manual_price_verification_priority") or []),
                "domain_top": dict(list((result.get("domain_performance") or {}).items())[:8]),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
