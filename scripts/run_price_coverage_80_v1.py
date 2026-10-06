"""Run price accuracy + 80% coverage validation.

  python scripts/run_price_coverage_80_v1.py
  python scripts/run_price_coverage_80_v1.py --stage easy25 --seconds 280
  python scripts/run_price_coverage_80_v1.py --fresh --rebuild-corpus
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
    ap.add_argument("--stage", choices=["all", "easy25", "full", "control"], default="all")
    ap.add_argument("--rebuild-corpus", action="store_true")
    ap.add_argument("--verify-corpus", action="store_true")
    args = ap.parse_args()

    from m3_data_root import data_path
    from price_coverage_80.sweep import format_completion_report, run_price_coverage_80

    if args.fresh:
        p = data_path("m3_price_coverage_80_checkpoint.json")
        if p.exists():
            p.unlink()

    def progress(**kw):
        print(f"  {kw.get('pct', 0):3}% {kw.get('phase')} n={kw.get('n')}", flush=True)

    report = run_price_coverage_80(
        max_seconds=args.seconds,
        resume=not args.fresh,
        stage=args.stage,
        rebuild_corpus=args.rebuild_corpus,
        verify_corpus=args.verify_corpus,
        on_progress=progress,
    )
    print(format_completion_report(report))
    print(f"\nReport: {data_path('m3_price_coverage_80_last_report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
