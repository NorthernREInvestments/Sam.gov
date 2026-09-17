"""Run transactional procurement intelligence on live Iowa SciQuest deals."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from transactional_procurement import (  # noqa: E402
    PRIORITY_SOLICITATIONS,
    run_transactional_procurement,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="Authorize live HTTP")
    p.add_argument("--max-http", type=int, default=50)
    p.add_argument("--target-http", type=int, default=35)
    p.add_argument(
        "--solicitations",
        nargs="*",
        default=None,
        help="Override solicitation list (default: blades, seed, badges)",
    )
    args = p.parse_args()
    sols = args.solicitations or PRIORITY_SOLICITATIONS
    results = run_transactional_procurement(
        authorize_live=args.live,
        solicitations=list(sols),
        max_http=args.max_http,
        target_http=args.target_http,
    )
    print(json.dumps(
        {
            "analyzed": results.get("analyzed"),
            "request_counts": results.get("request_counts"),
            "next_state": results.get("next_state"),
        },
        indent=2,
        default=str,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
