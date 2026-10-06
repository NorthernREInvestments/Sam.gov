"""Staged OpenGov public document recovery (20 → 100 → 500 → full)."""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-entities", type=int, default=80)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-line-items", action="store_true")
    args = ap.parse_args()

    from opengov_recovery.public_docs_batch import run_opengov_public_docs_stage

    def on_progress(**kwargs):
        print("PROGRESS", json.dumps(kwargs, default=str), flush=True)

    report = run_opengov_public_docs_stage(
        limit=args.limit,
        download=not args.no_download,
        resume=not args.no_resume,
        max_entities=args.max_entities,
        on_progress=on_progress,
        handoff_line_items=not args.no_line_items,
    )
    print("REPORT", json.dumps({k: report.get(k) for k in report if k != "samples"}, indent=2, default=str)[:5000])
    print("SAMPLES", json.dumps(report.get("samples") or [], indent=2, default=str)[:3000])
    # Stop-scale guard
    attempted = int(report.get("attempted") or 0)
    found = int(report.get("valid_or_partial") or 0)
    if attempted >= 20 and found == 0:
        print("STOP_SCALE: zero valid/partial packages after", attempted, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
