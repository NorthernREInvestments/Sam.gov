"""CLI: Phase I executable live deal hunt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase_i.hunt import run_phase_i_hunt


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Phase I live deal hunt")
    p.add_argument("--no-expand", action="store_true")
    p.add_argument("--no-keywords", action="store_true")
    p.add_argument("--deep-max", type=int, default=50)
    p.add_argument("--stage-c", type=int, default=50)
    p.add_argument("--quote-target", type=int, default=3)
    p.add_argument("--sam-calls", type=int, default=20)
    p.add_argument("--keyword-calls", type=int, default=12)
    p.add_argument("--days-back", type=int, default=30)
    p.add_argument("--offline", action="store_true", help="Seed-only, no live HTTP deep fetch")
    args = p.parse_args()
    out = run_phase_i_hunt(
        expand_sam=not args.no_expand and not args.offline,
        keyword_hunt=not args.no_keywords and not args.offline,
        sam_expand_calls=args.sam_calls,
        keyword_calls=args.keyword_calls,
        days_back=args.days_back,
        stage_c_cap=args.stage_c,
        deep_max=args.deep_max,
        quote_ready_target=args.quote_target,
        authorize_live=not args.offline,
    )
    print(json.dumps(out["scorecard"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
