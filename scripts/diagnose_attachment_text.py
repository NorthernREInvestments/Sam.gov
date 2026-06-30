"""One-off: diagnose attachment_text storage in live DB."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from models import Contract
from sam_enrich import is_scrape_complete
from claude_client import contract_attachment_text


def main() -> None:
    session = SessionLocal()
    try:
        rows = session.query(Contract).order_by(Contract.id).all()
        print(f"Total contracts: {len(rows)}\n")

        # Check if DB column exists
        from sqlalchemy import inspect

        insp = inspect(session.bind)
        cols = [c["name"] for c in insp.get_columns("contracts")]
        print("Contract columns with attachment/subcontract:", [c for c in cols if "attachment" in c or "subcontract" in c])
        print()

        for row in rows:
            has_col = hasattr(row, "attachment_text")
            stored = getattr(row, "attachment_text", None) if has_col else None
            analysis = row.analysis if isinstance(row.analysis, dict) else {}
            scrape = is_scrape_complete(row.sam_raw if isinstance(row.sam_raw, dict) else None)
            print(f"--- {row.notice_id}")
            print(f"  title: {(row.title or '')[:70]}")
            print(f"  scrape_complete: {scrape}")
            print(f"  DB attachment_text column: {has_col}")
            if has_col:
                print(f"  stored attachment_text chars: {len(stored) if stored else 0}")
            print(f"  analysis.attachment_text chars: {len(analysis.get('attachment_text') or '')}")
            try:
                live = contract_attachment_text(row, max_pdfs=12)
                print(f"  live extract (on-demand) chars: {len(live)}")
            except Exception as exc:
                print(f"  live extract error: {exc}")

        ashe = [
            r
            for r in rows
            if "asheville" in (r.title or "").lower()
            or "southern research" in (r.title or "").lower()
            or "1240BE26Q0083" in (r.description or "")
            or "1240BE26Q0083" in str(r.sam_raw or "")
        ]
        print("\n=== Asheville / 1240BE26Q0083 matches ===")
        for row in ashe:
            print(f"notice_id: {row.notice_id}")
            print(f"title: {row.title}")
            stored = getattr(row, "attachment_text", None)
            if stored:
                print(f"stored preview ({len(stored)} chars):\n{stored[:500]}")
            else:
                print("stored attachment_text: NULL/empty (column may not exist)")
                live = contract_attachment_text(row, max_pdfs=12)
                print(f"on-demand extract: {len(live)} chars")
                if live:
                    print(f"preview:\n{live[:500]}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
