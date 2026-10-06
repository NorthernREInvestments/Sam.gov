"""Run acquisition-cost scale on the same 100-opportunity sample.

  python scripts/run_acquisition_scale_v1.py
  python scripts/run_acquisition_scale_v1.py --seconds 280
  python scripts/run_acquisition_scale_v1.py --fresh
  python scripts/run_acquisition_scale_v1.py --line-cap 4
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
    ap.add_argument("--seconds", type=float, default=280.0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--line-cap", type=int, default=4)
    args = ap.parse_args()

    from acquisition_scale.sweep import format_completion_report, run_acquisition_scale_v1
    from m3_data_root import data_path

    if args.fresh:
        p = data_path("m3_acquisition_scale_v1_checkpoint.json")
        if p.exists():
            p.unlink()

    def progress(**kw):
        print(f"  {kw.get('pct', 0):3}% {kw.get('phase')} opp={kw.get('opp')}", flush=True)

    report = run_acquisition_scale_v1(
        max_seconds=args.seconds,
        per_opp_line_cap=args.line_cap,
        resume=not args.fresh,
        on_progress=progress,
    )
    print(format_completion_report(report))
    print(f"\nReport: {data_path('m3_acquisition_scale_v1_last_report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
