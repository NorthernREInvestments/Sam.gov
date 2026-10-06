"""Run revenue evidence recovery on the same 100-opportunity sample.

  python scripts/run_revenue_evidence_v1.py
  python scripts/run_revenue_evidence_v1.py --seconds 280
  python scripts/run_revenue_evidence_v1.py --fresh --no-package-mine
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
    ap.add_argument("--no-package-mine", action="store_true", help="Skip R1 PDF mining (faster)")
    args = ap.parse_args()

    from m3_data_root import data_path
    from revenue_evidence.sweep import format_completion_report, run_revenue_evidence_v1

    if args.fresh:
        p = data_path("m3_revenue_evidence_v1_checkpoint.json")
        if p.exists():
            p.unlink()

    def progress(**kw):
        print(f"  {kw.get('pct', 0):3}% {kw.get('phase')} opp={kw.get('opp')}", flush=True)

    report = run_revenue_evidence_v1(
        max_seconds=args.seconds,
        resume=not args.fresh,
        mine_package=not args.no_package_mine,
        on_progress=progress,
    )
    print(format_completion_report(report))
    print(f"\nReport: {data_path('m3_revenue_evidence_v1_last_report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
