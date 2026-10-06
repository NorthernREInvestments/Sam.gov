"""Run controlled price-search reliability + eligibility applicability audit.

Usage:
  python scripts/run_price_search_reliability_v1.py
  python scripts/run_price_search_reliability_v1.py --exact 40 --seconds 180
  python scripts/run_price_search_reliability_v1.py --fresh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exact", type=int, default=100)
    ap.add_argument("--spec", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=240.0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--force-spec", action="store_true")
    args = ap.parse_args()

    from eligibility_and_recovery.price_reliability_v1 import (
        format_completion_report,
        run_price_search_reliability_v1,
    )
    from m3_data_root import data_path

    if args.fresh:
        for name in (
            "m3_price_search_reliability_v1_checkpoint.json",
            "m3_price_route_circuits.json",
        ):
            p = data_path(name)
            if p.exists():
                p.unlink()

    def progress(**kw):
        print(
            f"  {kw.get('pct', 0):3}% {kw.get('phase')} "
            f"opp={kw.get('opp')} prices={kw.get('prices')}",
            flush=True,
        )

    report = run_price_search_reliability_v1(
        exact_n=args.exact,
        spec_n=args.spec,
        max_seconds=args.seconds,
        run_spec=True if args.force_spec else None,
        resume=not args.fresh,
        on_progress=progress,
    )
    print(format_completion_report(report))
    print(f"\nReport saved: {data_path('m3_price_search_reliability_v1_last_report.json')}")
    if report.get("stop_scale"):
        print("STOP SCALE: manual benchmark <50% — do not scale full population.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
