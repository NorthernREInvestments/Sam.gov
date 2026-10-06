"""Run large 500-opportunity production test.

  python scripts/run_large_production_test_v1.py
  python scripts/run_large_production_test_v1.py --fresh
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from large_production_test.models import BUILD, REPORT
    from large_production_test.sweep import run_large_production_test_v1
    from m3_data_root import data_path

    fresh = "--fresh" in sys.argv
    print(f"=== {BUILD} ===", flush=True)
    report = run_large_production_test_v1(resume=not fresh)
    print(f"\nWrote {data_path(REPORT)}", flush=True)
    print(f"LARGE_TEST_PASS={report.get('LARGE_TEST_PASS')}", flush=True)
    print(f"SAFE_TO_FULL_SCALE={report.get('SAFE_TO_FULL_SCALE')}", flush=True)
    print(f"NEXT_RUN_ALLOWED={report.get('NEXT_RUN_ALLOWED')}", flush=True)
    print(f"REAL_SUPPLIER_LOOP_PROVEN={report.get('REAL_SUPPLIER_LOOP_PROVEN')}", flush=True)
    return 0 if report.get("LARGE_TEST_PASS") == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
