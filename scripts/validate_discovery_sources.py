#!/usr/bin/env python3
"""
Validate discovery sources with controlled live HTTP.

Does NOT persist opportunities.
MAY persist registry validation status.

  python scripts/validate_discovery_sources.py --authorize-live --max-sources 15 --max-requests-per-source 2

SAM=0 OpenAI=0 USAspending=0 paid=0
Max 30 public procurement HTTP requests.
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--max-sources", type=int, default=15)
    parser.add_argument("--max-requests-per-source", type=int, default=2)
    parser.add_argument("--max-total-requests", type=int, default=30)
    parser.add_argument("--no-persist-registry", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from discovery.coverage import build_coverage_report
    from discovery.registry import seed_discovery_sources
    from discovery.sam_policy import assert_no_broad_sam_discovery, sam_api_broad_discovery_enabled
    from discovery.validate import validate_sources

    print("=== Discovery Source Validation ===")
    print(f"SAM_API_BROAD_DISCOVERY_ENABLED={sam_api_broad_discovery_enabled()}")
    print(assert_no_broad_sam_discovery())
    if sam_api_broad_discovery_enabled():
        print("REFUSING: broad SAM discovery enabled")
        return 2
    if not args.authorize_live:
        print("Pass --authorize-live to contact public sources. Zero HTTP.")
        return 0

    from database import SessionLocal

    session = SessionLocal()
    try:
        seed_discovery_sources(session)
        session.commit()
        out = validate_sources(
            session,
            authorize_live=True,
            max_sources=args.max_sources,
            max_requests_per_source=args.max_requests_per_source,
            max_total_requests=args.max_total_requests,
            persist_registry=not args.no_persist_registry,
        )
        session.commit()
        for r in out.get("results") or []:
            print(
                f"- {r.get('source_id')}: status={r.get('adapter_status')} "
                f"http={r.get('http_status')} records={r.get('records_visible')} "
                f"req={r.get('requests')} sample={r.get('sample_title')!r} "
                f"url={r.get('list_url')}"
            )
            if r.get("error"):
                print(f"  error={r['error']}")
        print(f"\nLIVE_VERIFIED={out.get('LIVE_VERIFIED')} ids={out.get('verified_source_ids')}")
        acct = out.get("accounting") or {}
        print(
            f"requests={acct.get('request_count')} SAM={out.get('SAM')} "
            f"OpenAI={out.get('OpenAI')} USAspending={out.get('USAspending')} paid={out.get('paid')}"
        )
        cov = build_coverage_report(session)
        print(f"coverage LIVE_VERIFIED={cov['TOTAL_LIVE_VERIFIED_SOURCES']} UNVERIFIED={cov['TOTAL_UNVERIFIED_LIVE_SOURCES']}")
        if args.json:
            print(json.dumps(out, indent=2, default=str))
        return 0
    except Exception as exc:
        session.rollback()
        print(f"ERROR: {exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
