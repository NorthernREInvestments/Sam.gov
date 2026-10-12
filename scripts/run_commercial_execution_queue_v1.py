#!/usr/bin/env python3
"""Run automated commercial execution with hard budget control (default: $0 OpenAI)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from buyer_intelligence.commercial_execution_queue_v1 import run_acceptance

    out = run_acceptance(max_opps=20, allow_openai=False)
    r = out["report"]
    print("B_BUDGET", json.dumps(r["B_BUDGET"], indent=2, default=str)[:1200])
    print("C_EXEC", json.dumps(r["C_EXECUTION"], indent=2))
    print("I", json.dumps(r["I_BUSINESS"], indent=2, default=str)[:800])
    print("RFQS", out["rfqs"].get("COUNT"), "DEALS", out["deals"].get("count"))
    print("WALL", r.get("WALL_S"), "DEPLOY", r.get("DEPLOYMENT"))
    # Fail if any OpenAI spent
    if str(r["B_BUDGET"].get("openai_actual_spend_this_run_usd")) not in {"0", "0.0", "0.00", "0.0000"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
