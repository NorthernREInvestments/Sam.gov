"""Dry-run auditor for legacy fact-like fields.

Default: DRY RUN — inspect only, mutate ZERO production records.
Never prints secrets. No external API calls.

Usage:
  python scripts/audit_legacy_data_integrity.py
  python scripts/audit_legacy_data_integrity.py --limit 500
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit legacy data integrity (dry-run by default)")
    parser.add_argument("--limit", type=int, default=None, help="Max contracts to inspect")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="FORBIDDEN in this task — apply is disabled; dry-run only",
    )
    args = parser.parse_args()

    if args.apply:
        print(json.dumps({"error": "apply_disabled", "message": "Mutation not authorized — dry-run only"}))
        return 2

    from database import SessionLocal
    from legacy_data_integrity import audit_contract_record, summarize_findings
    from models import Contract

    session = SessionLocal()
    rows_modified = 0
    try:
        q = session.query(Contract).order_by(Contract.id.asc())
        if args.limit:
            q = q.limit(args.limit)
        contracts = q.all()

        all_findings: list[dict] = []
        fields_seen: set[str] = set()
        null_counts = {
            "estimated_value": 0,
            "square_footage": 0,
            "awarded_amount": 0,
            "price_per_sqft_per_year": 0,
            "price_per_sqft_per_visit": 0,
            "cleaning_frequency_per_week": 0,
            "margin_percentage": 0,
            "selected_sub_quote": 0,
            "analysis.estimated_value": 0,
        }
        for row in contracts:
            analysis = row.analysis if isinstance(row.analysis, dict) else {}
            for key, attr in (
                ("estimated_value", "estimated_value"),
                ("square_footage", "square_footage"),
                ("awarded_amount", "awarded_amount"),
                ("price_per_sqft_per_year", "price_per_sqft_per_year"),
                ("price_per_sqft_per_visit", "price_per_sqft_per_visit"),
                ("cleaning_frequency_per_week", "cleaning_frequency_per_week"),
                ("margin_percentage", "margin_percentage"),
                ("selected_sub_quote", "selected_sub_quote"),
            ):
                if getattr(row, attr, None) in (None, ""):
                    null_counts[key] += 1
            if analysis.get("estimated_value") in (None, ""):
                null_counts["analysis.estimated_value"] += 1

            findings = audit_contract_record(row)
            for f in findings:
                fields_seen.add(str(f.get("field")))
            all_findings.extend(findings)

        summary = summarize_findings(all_findings)
        report = {
            "mode": "DRY_RUN",
            "database_rows_modified": rows_modified,
            "records_inspected": len(contracts),
            "fields_inspected": sorted(fields_seen | set(null_counts.keys())),
            "null_or_absent_counts": null_counts,
            "summary": summary,
            "live_api_requests": 0,
        }
        print("LEGACY_DATA_INTEGRITY_AUDIT")
        print(json.dumps(report, indent=2, default=str))
        print("DATABASE_ROWS_MODIFIED", rows_modified)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
