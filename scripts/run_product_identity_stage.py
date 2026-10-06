"""Staged product-identity run: 500 → 5000 → all confirmed/likely lines."""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line-limit", type=int, default=500)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-handoff", action="store_true")
    ap.add_argument("--max-packages", type=int, default=None)
    args = ap.parse_args()

    from product_identity.batch import run_product_identity_stage

    def on_progress(**kwargs):
        print("PROGRESS", json.dumps(kwargs, default=str), flush=True)

    report = run_product_identity_stage(
        line_limit=args.line_limit,
        resume=not args.no_resume,
        handoff=not args.no_handoff,
        on_progress=on_progress,
        max_packages=args.max_packages,
    )
    print("REPORT", json.dumps({k: report.get(k) for k in report if k != "samples"}, indent=2, default=str)[:7000])
    print("SAMPLES", json.dumps(report.get("samples") or [], indent=2, default=str)[:2500])
    usable = int((report.get("PACKAGE_LEVEL") or {}).get("Packages with >=1 A/B/C identity") or 0)
    abc = sum(int((report.get("CONFIDENCE") or {}).get(g) or 0) for g in ("A", "B", "C"))
    if int(report.get("line_limit") or 0) >= 500 and abc == 0:
        print("STOP_SCALE: zero A/B/C identities", file=sys.stderr)
        return 2
    print(f"USABLE_ABC={abc} PACKAGES_WITH_IDENTITY={usable}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
