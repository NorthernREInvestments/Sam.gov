#!/usr/bin/env python3
"""Transactional resale discovery — local re-rank + optional controlled expansion + deep research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--allow-openai", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    from transactional_discovery import run_transactional_discovery, write_transactional_artifacts

    print("=== Transactional Resale Discovery ===")
    result = run_transactional_discovery(
        authorize_live=bool(args.authorize_live),
        force_openai_offline=not args.allow_openai,
    )
    er = result.get("existing_rerank") or {}
    print(f"Existing pool: {er.get('total')} | Tier A/B: {result.get('tier_ab_count')}")
    print(f"Old product: {er.get('old_product_counts')}")
    print(f"New effective: {er.get('new_product_effective_counts')}")
    print(f"Tiers: {er.get('launch_tier_counts')}")
    print(f"UNKNOWN listing reduced: {er.get('unknown_listing_reduced')}")
    print(f"Expansion: {result.get('expansion')}")
    print(f"HTTP: {result.get('LIVE_API_REQUESTS')} OpenAI: {result.get('OpenAI')} SAM: 0")
    print(f"Transactional candidates: {len(result.get('transactional_candidates') or [])}")
    print(f"Deep researched: {len(result.get('deep_research_results') or [])}")
    print(f"Replaced: {len(result.get('deep_research_rejected_replaced') or [])}")
    for o in (result.get("transactional_candidates") or [])[:10]:
        print(f"  - [{o.get('launch_tier')}] {o.get('source_id')} | {(o.get('title') or '')[:70]}")
    for p in result.get("deep_research_results") or []:
        opp = p.get("opportunity") or {}
        print(f"  DEEP {opp.get('solicitation_id')}: {p.get('deal_type')} -> {p.get('decision')} / {p.get('primary_next_action')}")

    if not args.no_artifacts:
        paths = write_transactional_artifacts(result)
        print(f"Artifacts: {paths}")
    if args.json:
        # avoid dumping huge all_ranked
        slim = {k: v for k, v in result.items() if k != "all_ranked"}
        print(json.dumps(slim, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
