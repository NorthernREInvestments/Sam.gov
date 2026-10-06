"""Run P1 pre-scale hardening build.

  python scripts/run_p1_prescale_hardening_v1.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from p1_prescale_hardening.models import BUILD, REPORT
    from p1_prescale_hardening.sweep import format_report, run_p1_prescale_hardening_v1
    from m3_data_root import data_path

    print(f"=== {BUILD} ===", flush=True)
    report = run_p1_prescale_hardening_v1()
    text = format_report(report)
    out = data_path(REPORT).with_suffix(".txt")
    out.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"\nWrote {data_path(REPORT)}", flush=True)
    print(f"Wrote {out}", flush=True)
    return 0 if report.get("SAFE_FOR_LARGE_TEST") == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
