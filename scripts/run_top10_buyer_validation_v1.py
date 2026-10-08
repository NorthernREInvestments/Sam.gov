"""Canonical runner — Top-10 Buyer Validation V1."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from buyer_intelligence.validate_top10 import BUILD, run_top10_validation

    print("BUILD", BUILD, flush=True)
    t0 = time.perf_counter()
    report = run_top10_validation()
    print("STATUS", report.get("STATUS"), "elapsed", round(time.perf_counter() - t0, 2), flush=True)
    print("COUNTS", json.dumps(report.get("counts"), indent=2), flush=True)
    print("GATES", json.dumps(report.get("gates"), indent=2), flush=True)
    for t in report.get("targets") or []:
        print(
            f"{t.get('rank')}. {t.get('DISPOSITION'):12} | {t.get('BUYER_NAME')[:40]:40} | "
            f"{t.get('CATEGORY'):18} | purchases={t.get('TOTAL_DISTINCT_PURCHASES')} "
            f"med_order={t.get('MEDIAN_ORDER_TOTAL')} cash={t.get('CASH_RISK')} "
            f"conf={t.get('BUYER_NORMALIZATION_CONFIDENCE')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
