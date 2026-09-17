#!/usr/bin/env python3
"""
REAL TINY end-to-end launch validation.

Runs live public discovery (≤25 HTTP) through full M3 pipeline:
discovery → structural → product → deadline → executability → economics → funding → operator queue.

Requires explicit --authorize-live. SAM=0 OpenAI=0 USAspending=0 paid=0.
No lender outreach.
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
    parser = argparse.ArgumentParser(description="REAL TINY end-to-end validation")
    parser.add_argument("--authorize-live", action="store_true", help="Required for live HTTP")
    parser.add_argument("--json", action="store_true", help="Print full JSON result")
    parser.add_argument("--no-artifacts", action="store_true", help="Skip writing artifact files")
    args = parser.parse_args()

    from discovery.sam_policy import assert_no_broad_sam_discovery
    from discovery.tiny_end_to_end import run_tiny_end_to_end, write_tiny_artifacts

    policy = assert_no_broad_sam_discovery()
    if not policy.get("broad_discovery_blocked"):
        print("REFUSING: SAM broad discovery must remain blocked")
        return 2

    if not args.authorize_live:
        print("REAL TINY requires --authorize-live for controlled public HTTP.")
        print("SAM=0 OpenAI=0 USAspending=0 paid=0 lender_outreach=0")
        return 0

    print("=== REAL TINY End-to-End Validation ===")
    result = run_tiny_end_to_end(authorize_live=True)
    funnel = result.get("funnel") or {}

    print(f"HTTP requests: {result.get('LIVE_API_REQUESTS', 0)}")
    print(f"Sources attempted: {funnel.get('sources_attempted')} successful: {funnel.get('sources_successful')}")
    print(f"Raw: {funnel.get('raw_listings')} valid: {funnel.get('structurally_valid')} deduped: {funnel.get('deduped_count')}")
    print(f"Survivors: {funnel.get('surviving_operator_candidates')}")
    print(f"Top candidates: {len(result.get('top_candidates') or [])}")

    artifacts = {}
    if not args.no_artifacts:
        artifacts = write_tiny_artifacts(result)
        print(f"Artifacts: {artifacts}")

    if args.json:
        print(json.dumps(result, indent=2, default=str))

    if result.get("LIVE_API_REQUESTS", 0) > 25:
        print("WARNING: exceeded hard max 25 public HTTP requests")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
