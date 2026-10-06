"""Run package recovery + SAM budget build on LARGE_TEST_CORPUS_V1.

  python scripts/run_package_recovery_sam_budget_v1.py
  python scripts/run_package_recovery_sam_budget_v1.py --fresh
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from package_recovery_sam_budget.models import BUILD, REPORT
    from package_recovery_sam_budget.sweep import run_package_recovery_sam_budget_v1
    from m3_data_root import data_path

    fresh = "--fresh" in sys.argv
    print(f"=== {BUILD} ===", flush=True)
    report = run_package_recovery_sam_budget_v1(resume=not fresh, force_bidnet=True)
    print(f"\nWrote {data_path(REPORT)}", flush=True)
    print(f"PACKAGE_RECOVERY_PASS={report.get('PACKAGE_RECOVERY_PASS')}", flush=True)
    print(f"NEXT_RUN_ALLOWED={report.get('NEXT_RUN_ALLOWED')}", flush=True)
    print(f"after_packages={report.get('after_packages')} net_new={report.get('net_new')}", flush=True)
    return 0 if report.get("PACKAGE_RECOVERY_PASS") == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
