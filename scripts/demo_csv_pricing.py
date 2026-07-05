"""Demo CSV pricing lookup on a few gt_csv_opportunities rows."""

from __future__ import annotations

from database import SessionLocal
from csv_pricing_service import lookup_csv_opportunity_pricing
from models import CsvOpportunity


def main() -> None:
    session = SessionLocal()
    try:
        total = session.query(CsvOpportunity).count()
        print(f"CSV rows in DB: {total}")
        rows = (
            session.query(CsvOpportunity)
            .order_by(CsvOpportunity.due_date.asc().nullslast())
            .limit(5)
            .all()
        )
        if not rows:
            print("No CSV opportunities in this database.")
            return
        for row in rows:
            agency = (row.agency or "")[:60]
            print("\n---")
            print(f"Title: {row.title[:80]}")
            print(f"NAICS: {row.naics_code} | {row.location_city}, {row.location_state} | {agency}")
            result = lookup_csv_opportunity_pricing(row)
            display = result.get("pricing_display") or {}
            intel = result.get("pricing_intel") or {}
            pred = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
            print(f"Card: {display.get('main_line')}")
            print(f"Source: {display.get('source_label')} | Prior bidders: {display.get('unique_bidders')}")
            print(f"Method: {display.get('lookup_method') or pred.get('lookup_method')}")
            print(f"Lookback: {intel.get('lookback_years')} years")
            if intel.get("error"):
                print(f"Error: {intel.get('error')}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
