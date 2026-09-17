#!/usr/bin/env python3
"""Deep Deal Research + Bid Qualification — controlled sample run.

Requires --authorize-live for public HTTP document fetches.
Default OpenAI offline unless --allow-openai.
SAM=0 USAspending=0 paid=0 lender/supplier outreach=0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep deal research on TINY top sample")
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--allow-openai", action="store_true", help="Permit OpenAI (target <=10, hard max 15)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    from deep_deal_research import run_deep_deal_research, write_deep_deal_artifacts

    print("=== Deep Deal Research + Bid Qualification ===")
    result = run_deep_deal_research(
        authorize_live=bool(args.authorize_live),
        force_openai_offline=not args.allow_openai,
        max_candidates=5,
    )
    budgets = result.get("budgets") or {}
    print(f"Candidates: {result.get('candidate_count')}")
    print(f"Public HTTP: {result.get('LIVE_API_REQUESTS')} OpenAI: {result.get('OpenAI')} SAM: 0")
    print(f"Queues: {json.dumps(result.get('queues'), indent=2)}")
    print(f"Cheap UNKNOWN improvements: {result.get('cheap_unknown_improved_count')}")

    for r in result.get("results") or []:
        opp = r.get("opportunity") or {}
        print(
            f"- {opp.get('solicitation_id')}: type={r.get('deal_type')} "
            f"fit={r.get('pre_deep_fit')} decision={r.get('decision')} next={r.get('primary_next_action')}"
        )

    if not args.no_artifacts:
        paths = write_deep_deal_artifacts(result)
        print(f"Artifacts: {paths}")

    if args.json:
        print(json.dumps(result, indent=2, default=str))

    if (budgets.get("public_http") or 0) > 80:
        print("WARNING: exceeded public HTTP hard max")
        return 3
    if (budgets.get("openai_requests_used") or 0) > 15:
        print("WARNING: exceeded OpenAI hard max")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
