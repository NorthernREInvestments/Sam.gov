"""Canonical runner for Buyer Intelligence + Micro-Purchase V1."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from buyer_intelligence.engine import BUILD, run_buyer_intelligence

    t0 = time.perf_counter()
    print(f"BUILD {BUILD}", flush=True)
    report = run_buyer_intelligence()
    elapsed = round(time.perf_counter() - t0, 2)
    counts = report.get("counts") or {}
    gates = report.get("gates") or {}
    print("STATUS", report.get("STATUS"), "elapsed_s", elapsed, flush=True)
    print("COUNTS", json.dumps(counts, indent=2), flush=True)
    print("GATES", json.dumps(gates, indent=2), flush=True)
    print("TOP5", flush=True)
    for t in (report.get("TARGET_BUYERS") or [])[:5]:
        print(
            f"  {t.get('BUYER_NAME_CANONICAL')} | {t.get('product_family')} | "
            f"buys={t.get('BUY_COUNT')} med={t.get('MEDIAN_BUY')} "
            f"score={t.get('TARGET_ACCOUNT_SCORE')} cash={t.get('ESTIMATED_OWNER_CASH_RISK')} "
            f"action={t.get('NEXT_ACTION')}",
            flush=True,
        )
    return 0 if report.get("STATUS") in {"PASS", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
