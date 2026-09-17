#!/usr/bin/env python3
"""
Controlled live public discovery — PREVIEW (default) or --persist.

DO NOT run during automated builds without explicit operator authorization.

Examples (DO NOT RUN unless authorized):

  # TINY PREVIEW — no persist, contacts live sources
  python scripts/run_public_discovery_smoke.py --profile tiny --preview --authorize-live

  # BROAD PREVIEW
  python scripts/run_public_discovery_smoke.py --profile broad --preview --authorize-live

  # TINY with persistence (explicit)
  python scripts/run_public_discovery_smoke.py --profile tiny --persist --authorize-live

Enforced: SAM=0 OpenAI=0 USAspending=0 paid=0
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
    parser = argparse.ArgumentParser(description="Public discovery preview/persist (operator-controlled)")
    parser.add_argument("--profile", choices=["tiny", "broad", "national"], default="tiny")
    parser.add_argument("--preview", action="store_true", default=True, help="Parse listings; do NOT persist (default)")
    parser.add_argument("--persist", action="store_true", help="Explicitly persist opportunities")
    parser.add_argument("--authorize-live", action="store_true", help="Required for any live HTTP")
    parser.add_argument("--source-id", action="append", dest="source_ids", default=None)
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = parser.parse_args()

    from discovery.coverage import build_coverage_report
    from discovery.live_runner import run_live_discovery
    from discovery.profiles import get_profile
    from discovery.sam_policy import assert_no_broad_sam_discovery, sam_api_broad_discovery_enabled

    policy = assert_no_broad_sam_discovery()
    prof = get_profile(args.profile)
    print("=== Public Discovery Controlled Run ===")
    print(f"SAM_API_BROAD_DISCOVERY_ENABLED={sam_api_broad_discovery_enabled()}")
    print(f"policy={policy}")
    print(f"profile={prof['name']} preview={not args.persist} persist={args.persist}")
    print(f"authorize_live={args.authorize_live}")
    print(f"budget={prof['budget'].to_dict()}")
    print(f"SAM={prof['SAM']} OpenAI={prof['OpenAI']} USAspending={prof['USAspending']} paid={prof['paid']}")

    if sam_api_broad_discovery_enabled():
        print("REFUSING: broad SAM discovery is enabled — turn it off before run.")
        return 2

    coverage = build_coverage_report()
    print(
        f"\nCoverage (architecture): LIVE_VERIFIED sources={coverage.get('TOTAL_LIVE_VERIFIED_SOURCES')} "
        f"states={coverage.get('TOTAL_LIVE_VERIFIED_STATES')} "
        f"agencies={coverage.get('TOTAL_LIVE_VERIFIED_LOCAL_AGENCIES')} "
        f"coops={coverage.get('TOTAL_LIVE_VERIFIED_COOPERATIVES')} "
        f"federal_non_sam={coverage.get('TOTAL_LIVE_VERIFIED_FEDERAL_NON_SAM')} "
        f"UNVERIFIED_LIVE={coverage.get('TOTAL_UNVERIFIED_LIVE_SOURCES')}"
    )

    if not args.authorize_live:
        print("\nNo --authorize-live: plan only. Zero HTTP.")
        print("external_request_counts: SAM=0 OpenAI=0 USAspending=0 paid=0 public_procurement=0")
        print("Authorize later with --authorize-live (still SAM=0 OpenAI=0).")
        return 0

    session = None
    if args.persist:
        from database import SessionLocal

        session = SessionLocal()

    try:
        out = run_live_discovery(
            session,
            profile=args.profile,
            preview=not args.persist,
            persist=args.persist,
            authorize_live=True,
            source_ids=args.source_ids,
        )
        if args.persist and session is not None:
            session.commit()

        m = out.get("metrics") or {}
        print("\n--- Results ---")
        print(f"sources_attempted={m.get('sources_attempted')} successful={m.get('sources_successful')} failed={m.get('sources_failed')}")
        print(f"raw_records={m.get('raw_records')} unique={m.get('unique_records')}")
        print(
            f"CORE_PRODUCT={m.get('CORE_PRODUCT')} PRODUCT_PLUS_SERVICE={m.get('PRODUCT_PLUS_SERVICE')} "
            f"UNKNOWN={m.get('UNKNOWN')} SERVICE={m.get('SERVICE')} expired={m.get('expired')}"
        )
        print(f"detail_fetches={m.get('detail_fetches')} documents={m.get('documents_discovered')}")
        print(f"preview={out.get('preview')} persist={out.get('persist')}")
        acct = out.get("accounting") or {}
        print(
            f"requests={acct.get('request_count')} cache_hits={acct.get('cache_hits')} "
            f"SAM={out.get('SAM')} OpenAI={out.get('OpenAI')} USAspending={out.get('USAspending')} paid={out.get('paid')}"
        )
        print("\nSources contacted:")
        for s in out.get("sources_contacted") or []:
            print(f"  - {s['source_id']}: {s.get('url')}")
        for sid, info in (m.get("per_source") or {}).items():
            print(f"  per_source[{sid}]={info}")
        if m.get("parser_warnings"):
            print(f"parser_warnings={m['parser_warnings'][:20]}")
        if args.json:
            print(json.dumps({k: v for k, v in out.items() if k != "opportunities"}, default=str, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        if session is not None:
            session.rollback()
        return 1
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
