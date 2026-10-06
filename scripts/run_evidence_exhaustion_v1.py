"""Run evidence exhaustion validation.

  python scripts/run_evidence_exhaustion_v1.py
  python scripts/run_evidence_exhaustion_v1.py --opps 100 --seconds 280
  python scripts/run_evidence_exhaustion_v1.py --fresh
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
    ap.add_argument("--opps", type=int, default=100)
    ap.add_argument("--lines", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=280.0)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    from evidence_exhaustion.sweep import format_completion_report, run_evidence_exhaustion_v1
    from m3_data_root import data_path

    if args.fresh:
        for name in (
            "m3_evidence_exhaustion_v1_checkpoint.json",
            "m3_price_route_circuits.json",
        ):
            p = data_path(name)
            if p.exists():
                p.unlink()

    def progress(**kw):
        print(
            f"  {kw.get('pct', 0):3}% {kw.get('phase')} opp={kw.get('opp')}",
            flush=True,
        )

    report = run_evidence_exhaustion_v1(
        max_opportunities=args.opps,
        per_opp_lines=args.lines,
        max_seconds=args.seconds,
        resume=not args.fresh,
        on_progress=progress,
    )
    print(format_completion_report(report))
    print(f"\nReport: {data_path('m3_evidence_exhaustion_v1_last_report.json')}")
    premature = (report.get("quote_reserve") or {}).get("premature_reserve")
    if premature:
        print(f"FAIL: premature reserve={premature}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
