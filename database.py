"""PostgreSQL connection setup."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing from .env")

# Railway URLs work with psycopg2 via the postgresql:// scheme.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_connection() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


def init_db() -> None:
    from models import AppSetting, Contract  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_add_sam_raw()


def _migrate_add_sam_raw() -> None:
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS sam_raw JSONB")
        )
        conn.commit()
