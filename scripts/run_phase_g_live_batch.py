"""CLI: Phase G supervised live batch."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase_g.live_batch import run_phase_g_live_batch


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--target", type=int, default=100)
    p.add_argument("--max-api-calls", type=int, default=8)
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--enrich-limit", type=int, default=0)
    args = p.parse_args()
    out = run_phase_g_live_batch(
        target=args.target,
        max_api_calls=args.max_api_calls,
        days_back=args.days_back,
        enrich_limit=args.enrich_limit or None,
    )
    sc = out["scorecard"]
    print(json.dumps(sc, indent=2, default=str))
    return 0 if sc.get("successfully_retrieved", 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
