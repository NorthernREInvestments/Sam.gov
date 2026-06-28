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
    from models import AppSetting, Contract, ContractSub, Proposal, Sub, SubcontractAgreement  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_add_sam_raw()
    _migrate_add_pricing_intel()
    _migrate_add_sub_finder()
    _migrate_add_internal_pricing()
    _migrate_add_proposals()
    _migrate_add_contract_tier()
    _migrate_add_sub_agreements()


def _migrate_add_sub_agreements() -> None:
    sub_columns = [
        ("owner_name", "VARCHAR(256)"),
        ("owner_title", "VARCHAR(128)"),
        ("license_number", "VARCHAR(128)"),
        ("insurance_carrier", "VARCHAR(256)"),
        ("business_email", "VARCHAR(256)"),
    ]
    with engine.connect() as conn:
        for name, col_type in sub_columns:
            conn.execute(text(f"ALTER TABLE subs ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                "ALTER TABLE contract_subs ADD COLUMN IF NOT EXISTS "
                "agreement_signature_status VARCHAR(64) DEFAULT 'Agreement Not Generated'"
            )
        )
        conn.execute(
            text("ALTER TABLE contract_subs ADD COLUMN IF NOT EXISTS agreement_status_log JSONB")
        )
        conn.commit()


def _migrate_add_contract_tier() -> None:
    from naics_labels import NAICS_TIER_BY_CODE

    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS tier INTEGER"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_contracts_tier ON contracts (tier)"))
        for code, tier in NAICS_TIER_BY_CODE.items():
            conn.execute(
                text("UPDATE contracts SET tier = :tier WHERE naics_code = :code AND tier IS NULL"),
                {"tier": tier, "code": code},
            )
        conn.commit()


def _migrate_add_proposals() -> None:
    """Proposals table created via create_all; no-op migration hook for future alters."""
    pass


def _migrate_add_internal_pricing() -> None:
    columns = [
        ("square_footage", "INTEGER"),
        ("building_type", "VARCHAR(32)"),
        ("cleaning_frequency_per_week", "NUMERIC(5, 2)"),
        ("special_requirements", "JSONB"),
        ("wage_determination_number", "VARCHAR(32)"),
        ("wage_determination_rate", "NUMERIC(8, 2)"),
        ("awarded_amount", "NUMERIC(14, 2)"),
        ("price_per_sqft_per_year", "NUMERIC(12, 6)"),
        ("price_per_sqft_per_visit", "NUMERIC(12, 6)"),
        ("pricing_region", "VARCHAR(8)"),
    ]
    with engine.connect() as conn:
        for name, col_type in columns:
            conn.execute(text(f"ALTER TABLE contracts ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.commit()


def _migrate_add_sub_finder() -> None:
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS selected_sub_quote NUMERIC(14, 2)")
        )
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS sub_search_status VARCHAR(32)")
        )
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS sub_search_radius_miles INTEGER")
        )
        conn.commit()


def _migrate_add_sam_raw() -> None:
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS sam_raw JSONB")
        )
        conn.commit()


def _migrate_add_pricing_intel() -> None:
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS pricing_intel JSONB")
        )
        conn.commit()
