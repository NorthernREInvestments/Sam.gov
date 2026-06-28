"""Read-only status check — no SAM API calls."""
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, text
import os

engine = create_engine(os.getenv("DATABASE_URL", ""))
with engine.connect() as conn:
    count = conn.execute(text("SELECT COUNT(*) FROM contracts")).scalar()
    print(f"contracts in db: {count}")

    usage = conn.execute(
        text("SELECT key, value FROM app_settings WHERE key LIKE 'sam_api_usage_%' ORDER BY key DESC LIMIT 5")
    ).fetchall()
    print("sam_api usage (recent days):")
    for key, value in usage:
        print(f"  {key}: {value}")

    rows = conn.execute(
        text(
            """
            SELECT notice_id, title,
                   sam_raw->>'scrapeStatus' AS scrape_status,
                   pricing_intel->>'recommended_annual_bid' AS recommended_bid,
                   pricing_intel->'unit_rate_summary'->>'rated_awards_count' AS rated_awards
            FROM contracts
            ORDER BY id
            LIMIT 10
            """
        )
    ).fetchall()
    if rows:
        print("contracts:")
        for row in rows:
            bid = f" bid={row[3]}" if row[3] else ""
            rated = f" rated={row[4]}" if row[4] is not None else ""
            print(f"  {row[0]} | {row[2]}{bid}{rated} | {row[1][:60]}")
