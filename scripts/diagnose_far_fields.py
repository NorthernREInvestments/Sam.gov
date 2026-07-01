import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import text

from database import SessionLocal, init_db, engine

init_db()

with engine.connect() as c:
    rows = c.execute(
        text(
            """
            SELECT column_name, data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'contracts'
              AND column_name IN (
                'attachment_text', 'subcontracting_limitation_check',
                'subcontracting_limitation_context', 'far_52219_14_present'
              )
            ORDER BY column_name
            """
        )
    ).fetchall()
    print("=== Schema columns ===")
    for r in rows:
        print(r)

session = SessionLocal()
from models import Contract

print("\n=== Contracts with attachment_text > 0 ===")
for row in session.query(Contract).order_by(Contract.id).all():
    chars = len(row.attachment_text or "")
    if chars <= 0:
        continue
    print(
        f"{(row.title or '')[:48]:<48} | chars={chars:>7} | "
        f"check={row.subcontracting_limitation_check!r} | "
        f"far_present={getattr(row, 'far_52219_14_present', 'NO_ATTR')!r}"
    )
session.close()
