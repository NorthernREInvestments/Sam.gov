"""Run BidNet official-source resolution sample batch."""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-line-items", action="store_true")
    args = ap.parse_args()

    from official_source.batch import run_official_source_batch

    def on_progress(**kwargs):
        print("PROGRESS", json.dumps(kwargs, default=str), flush=True)

    report = run_official_source_batch(
        limit=args.limit,
        download=not args.no_download,
        resume=not args.no_resume,
        on_progress=on_progress,
        handoff_line_items=not args.no_line_items,
    )
    print(
        "REPORT",
        json.dumps({k: report.get(k) for k in report if k != "samples"}, indent=2, default=str)[:6000],
    )
    attempted = int(report.get("attempted") or 0)
    resolved = int(report.get("official_source_resolved") or 0)
    if attempted >= 50 and resolved == 0:
        print("STOP_SCALE: zero official sources resolved", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
