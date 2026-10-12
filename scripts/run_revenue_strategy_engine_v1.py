#!/usr/bin/env python3
"""Run Revenue Strategy Engine V1 end-to-end validation."""

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
    from buyer_intelligence.revenue_strategy_engine_v1 import run_all

    out = run_all()
    r = out["report"]
    print("B_REPAIRS", r["B_LANE_REPAIRS"].get("count"), "corrections")
    print("D_PRICING", r["D_PRICING"])
    print("I_BUSINESS", r["I_BUSINESS"])
    print("C_MODE", r["C_STRATEGY"].get("mode"), "blockers", r["C_STRATEGY"].get("blocker_histogram"))
    print("G_N", len(r.get("G_IMMEDIATE") or []))
    print("WALL", r.get("WALL_S"), "DEPLOY", r.get("DEPLOYMENT"))
    # sanity: no verified scale from acquisition
    deals = out["deals"].get("deals") or []
    bad = [d for d in deals if d.get("lane") in {"SCALE", "GROWTH", "CASH_FLOW"} and d.get("lane_verification") != "VERIFIED"]
    print("BAD_LANES", len(bad))
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
