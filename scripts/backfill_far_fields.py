"""Re-run FAR 52.219-14 compliance on contracts that already have attachment_text."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from attachment_pipeline import rerun_subcontracting_check
from database import SessionLocal, init_db
from models import Contract


def main() -> None:
    init_db()
    session = SessionLocal()
    rows = (
        session.query(Contract)
        .filter(Contract.attachment_text.isnot(None), Contract.attachment_text != "")
        .order_by(Contract.id)
        .all()
    )
    print(f"Contracts with attachment_text: {len(rows)}")
    for row in rows:
        rerun_subcontracting_check(row)
    session.commit()

    print()
    print(f"{'Title':<60} | {'Chars':>8} | {'Check':<18} | far_52219_14_present")
    print("-" * 105)
    for row in rows:
        session.refresh(row)
        chars = len(row.attachment_text or "")
        check = row.subcontracting_limitation_check or "NULL"
        print(f"{(row.title or '')[:60]:<60} | {chars:>8} | {check:<18} | {row.far_52219_14_present}")
    session.close()


if __name__ == "__main__":
    main()
