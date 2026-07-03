"""Read-only database access for shared tables owned by other apps (e.g. gs_watchlist)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing from .env")

# Same Postgres instance as GovTracker — transactions forced read-only per connection.
shared_read_engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=3,
    max_overflow=5,
    connect_args={"connect_timeout": 15},
)


@event.listens_for(shared_read_engine, "connect")
def _set_read_only(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("SET default_transaction_read_only = ON")
    cursor.close()


SharedReadSession = sessionmaker(bind=shared_read_engine, autoflush=False, autocommit=False)


def resolve_watchlist_table_name() -> str | None:
    """Return gs_watchlist if present in public schema."""
    from db_tables import GS_WATCHLIST_TABLE

    with shared_read_engine.connect() as conn:
        reg = conn.execute(
            text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{GS_WATCHLIST_TABLE}"},
        ).scalar()
        if reg:
            return GS_WATCHLIST_TABLE
    return None
