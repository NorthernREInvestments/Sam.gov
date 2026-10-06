"""Run P0 pre-scale hardening.

  python scripts/run_p0_prescale_hardening_v1.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    from m3_data_root import data_path
    from p0_prescale_hardening.models import BUILD, REPORT
    from p0_prescale_hardening.sweep import format_report, run_p0_prescale_hardening_v1

    if args.report and data_path(REPORT).exists():
        print(format_report(json.loads(data_path(REPORT).read_text(encoding="utf-8"))))
        return 0

    print(f"Running {BUILD} ...", flush=True)
    report = run_p0_prescale_hardening_v1()
    print(format_report(report))
    print(f"\nWrote {data_path(REPORT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
