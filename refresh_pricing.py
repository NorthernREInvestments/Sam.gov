"""Backfill prior-contract extraction + USAspending pricing — no SAM.gov API calls."""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal
from models import Contract
from prior_contract_extract import backfill_prior_contract_and_pricing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract prior contract # / incumbent from stored PDFs and refresh USAspending pricing. No SAM.gov calls."
    )
    parser.add_argument("--notice-id", help="Refresh one contract by notice ID")
    parser.add_argument("--all", action="store_true", help="Refresh every contract")
    parser.add_argument(
        "--with-attachment-text-only",
        action="store_true",
        help="Only process contracts that have attachment_text or stored PDF bytes in the DB",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        query = session.query(Contract)
        if args.notice_id:
            query = query.filter_by(notice_id=args.notice_id)
        elif not args.all:
            parser.error("Pass --notice-id ID or --all")

        rows = query.order_by(Contract.id).all()
        if not rows:
            print("No matching contracts.")
            return

        print(f"Processing {len(rows)} contract(s) — SAM.gov will NOT be called.\n")

        stats = {
            "processed": 0,
            "prior_found": 0,
            "hints_in_pdf": 0,
            "reextracted": 0,
            "no_pdf_data": 0,
            "errors": 0,
        }

        for row in rows:
            has_text = bool(str(row.attachment_text or "").strip())
            if args.with_attachment_text_only and not has_text:
                from attachment_storage import stored_pdf_items

                if not row.id or not stored_pdf_items(session, row.id):
                    stats["no_pdf_data"] += 1
                    continue

            try:
                result = backfill_prior_contract_and_pricing(session, row)
                session.commit()
                stats["processed"] += 1

                if result.get("reextracted_from_db"):
                    stats["reextracted"] += 1
                if result.get("previous_contract_number") or result.get("incumbent_contractor"):
                    stats["hints_in_pdf"] += 1
                if result.get("is_prior_contract"):
                    stats["prior_found"] += 1

                kind = "prior" if result.get("is_prior_contract") else "regional avg"
                prior_no = result.get("previous_contract_number") or "—"
                incumbent = result.get("incumbent_contractor") or "—"
                print(
                    f"{row.notice_id[:12]}… | {kind} | "
                    f"annual={result.get('annual_amount')} | "
                    f"contract#={prior_no} | incumbent={incumbent[:40] if incumbent != '—' else '—'}"
                )
                pred = result.get("pricing_intel", {}).get("predecessor_award") or {}
                if isinstance(pred, dict) and pred.get("pricing_calc_note"):
                    print(f"  {pred['pricing_calc_note']}")
            except Exception as exc:
                session.rollback()
                stats["errors"] += 1
                print(f"{row.notice_id[:12]}… | ERROR | {exc}")

        print(
            f"\nDone: {stats['processed']} processed, "
            f"{stats['hints_in_pdf']} with PDF hints, "
            f"{stats['prior_found']} with USAspending prior match, "
            f"{stats['reextracted']} re-extracted from stored PDFs, "
            f"{stats['no_pdf_data']} skipped (no PDF data), "
            f"{stats['errors']} errors."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
