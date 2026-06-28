"""Clear cached pricing intel and refresh from USAspending (no SAM API calls)."""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal
from models import Contract
from pricing import get_contract_pricing_intel


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh USAspending pricing for contracts in Postgres.")
    parser.add_argument("--notice-id", help="Refresh one contract by notice ID")
    parser.add_argument("--all", action="store_true", help="Refresh every contract")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        query = session.query(Contract)
        if args.notice_id:
            query = query.filter_by(notice_id=args.notice_id)
        elif not args.all:
            parser.error("Pass --notice-id ID or --all")

        rows = query.all()
        if not rows:
            print("No matching contracts.")
            return

        for row in rows:
            row.pricing_intel = None
        session.commit()

        for row in rows:
            intel = get_contract_pricing_intel(row, force_refresh=True)
            session.commit()
            bid = intel.get("recommended_annual_bid")
            rated = (intel.get("unit_rate_summary") or {}).get("rated_awards_count", 0)
            print(f"{row.notice_id[:8]}… | awards={intel.get('awards_count')} rated={rated} | bid={bid}")
            if intel.get("recommended_bid_formula"):
                print(f"  {intel['recommended_bid_formula']}")
            elif intel.get("recommended_bid_note"):
                print(f"  {intel['recommended_bid_note']}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
