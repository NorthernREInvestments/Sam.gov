"""CLI: Phase H deep research on Phase G cohort."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase_h.deep_research import run_phase_h_deep_research


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--size", type=int, default=25)
    p.add_argument("--offline-docs", action="store_true", help="Skip live HTTP document fetch")
    args = p.parse_args()
    out = run_phase_h_deep_research(size=args.size, authorize_live=not args.offline_docs)
    print(json.dumps(out["scorecard"], indent=2, default=str))
    print("quote_ready", out["scorecard"].get("READY_FOR_QUOTE_OUTREACH"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
