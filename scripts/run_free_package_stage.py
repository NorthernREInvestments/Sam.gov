"""Run one free-package backlog stage with progress to stdout."""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--yield-floor", type=float, default=0.005)
    ap.add_argument("--min-gate", type=int, default=40)
    args = ap.parse_args()

    from bidnet_recovery.free_package_batch import (
        free_package_funnel_report,
        run_free_package_backlog,
    )

    def on_progress(**kwargs):
        print("PROGRESS", json.dumps(kwargs, default=str), flush=True)

    report = run_free_package_backlog(
        limit=args.limit,
        persist=True,
        resume=not args.no_resume,
        force=args.force,
        on_progress=on_progress,
        stop_if_yield_below=args.yield_floor,
        min_attempted_for_yield_gate=args.min_gate,
    )
    print("BATCH", json.dumps(report, indent=2, default=str)[:4000])
    funnel = free_package_funnel_report()
    print("FUNNEL", json.dumps(funnel, indent=2, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
