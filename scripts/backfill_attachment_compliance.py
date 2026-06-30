"""Backfill attachment text extraction and FAR 52.219-14 checks for all contracts."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from attachment_pipeline import run_attachment_pipeline
from database import SessionLocal, init_db
from models import Contract
from sam_enrich import is_sam_metadata_ready


def main() -> None:
    init_db()
    session = SessionLocal()
    rows = session.query(Contract).order_by(Contract.id).all()
    print(f"Backfilling {len(rows)} contract(s)...\n")
    print(f"{'Title':<55} | {'Before':>8} | {'After':>8} | {'Check':<18} | {'Method'}")
    print("-" * 110)

    for row in rows:
        before = len(row.attachment_text or "")
        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        if not is_sam_metadata_ready(raw):
            print(
                f"{(row.title or '')[:55]:<55} | {before:>8} | {before:>8} | {'SKIP_NO_METADATA':<18} | —"
            )
            continue
        try:
            summary = run_attachment_pipeline(row)
            session.commit()
            after = summary["attachment_text_chars"]
            check = summary["subcontracting_limitation_check"]
            method = summary["attachment_extraction_method"]
            print(f"{(row.title or '')[:55]:<55} | {before:>8} | {after:>8} | {check:<18} | {method}")
        except Exception as exc:
            session.rollback()
            print(f"{(row.title or '')[:55]:<55} | {before:>8} | ERROR    | {'ERROR':<18} | {exc}")

    session.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
