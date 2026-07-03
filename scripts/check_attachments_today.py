"""Check attachment download activity — read-only DB query."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, text
import os

from db_tables import GT_APP_SETTINGS, GT_CONTRACT_ATTACHMENTS, GT_CONTRACTS

engine = create_engine(os.getenv("DATABASE_URL", ""))
now = datetime.now(timezone.utc)
today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
# ~6 AM Mountain = 12:00 or 13:00 UTC depending on DST
mountain_morning = now.replace(hour=12, minute=0, second=0, microsecond=0)
if now < mountain_morning:
    mountain_morning = mountain_morning - timedelta(days=1)

with engine.connect() as conn:
    for label, start in [
        ("UTC midnight today", today_start),
        ("~6 AM Mountain today", mountain_morning),
        ("last 24 hours", now - timedelta(hours=24)),
    ]:
        row = conn.execute(
            text(
                f"""
                SELECT COUNT(*), MIN(downloaded_at), MAX(downloaded_at)
                FROM {GT_CONTRACT_ATTACHMENTS}
                WHERE downloaded_at >= :start
                """
            ),
            {"start": start},
        ).fetchone()
        print(f"=== Attachments since {label} ===")
        print(f"  count={row[0]}, first={row[1]}, last={row[2]}")

    recent = conn.execute(
        text(
            f"""
            SELECT ca.downloaded_at, ca.filename, c.notice_id, c.title
            FROM {GT_CONTRACT_ATTACHMENTS} ca
            JOIN {GT_CONTRACTS} c ON c.id = ca.contract_id
            WHERE ca.downloaded_at >= :start
            ORDER BY ca.downloaded_at DESC
            LIMIT 15
            """
        ),
        {"start": now - timedelta(hours=24)},
    ).fetchall()
    print("\n=== Recent downloads (24h) ===")
    if not recent:
        print("  (none)")
    for row in recent:
        title = (row[3] or "")[:50]
        fname = (row[1] or "")[:45]
        print(f"  {row[0]} | {fname} | {row[2][:12]} | {title}")

    usage = conn.execute(
        text(
            f"""
            SELECT key, value FROM {GT_APP_SETTINGS}
            WHERE key LIKE 'sam_api_usage_%' OR key LIKE 'sam_pdf%'
            ORDER BY key DESC LIMIT 10
            """
        )
    ).fetchall()
    print("\n=== SAM API usage (stored) ===")
    for key, value in usage:
        print(f"  {key}: {value}")

    scrape = conn.execute(
        text(
            f"""
            SELECT sam_raw->>'scrapeStatus' AS st, COUNT(*)
            FROM {GT_CONTRACTS}
            GROUP BY 1 ORDER BY 2 DESC
            """
        )
    ).fetchall()
    print("\n=== Scrape status (all contracts) ===")
    for st, n in scrape:
        print(f"  {st or 'null'}: {n}")

    pending = conn.execute(
        text(
            f"""
            SELECT COUNT(*) FROM {GT_CONTRACTS}
            WHERE sam_raw->>'scrapeStatus' IS DISTINCT FROM 'complete'
            """
        )
    ).scalar()
    print(f"\nContracts not fully scraped: {pending}")
