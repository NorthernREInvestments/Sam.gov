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
    from models import (  # noqa: F401
        AppSetting,
        Contract,
        ContractAttachment,
        ContractInvoice,
        ContractSub,
        Proposal,
        Sub,
        SubContact,
        SubPayment,
        SubcontractAgreement,
    )

    Base.metadata.create_all(bind=engine)
    _migrate_add_sam_raw()
    _migrate_add_pricing_intel()
    _migrate_add_sub_finder()
    _migrate_add_internal_pricing()
    _migrate_add_proposals()
    _migrate_add_contract_tier()
    _migrate_add_sub_agreements()
    _migrate_add_contract_margin()
    _migrate_add_attachment_compliance()
    _migrate_add_sub_contacts()
    _migrate_add_performance()
    _migrate_add_submission_package()


def _migrate_add_attachment_compliance() -> None:
    columns = [
        ("attachment_text", "TEXT"),
        ("attachment_extraction_method", "VARCHAR(32)"),
        ("attachment_extraction_note", "TEXT"),
        ("attachment_text_extracted_at", "TIMESTAMP WITH TIME ZONE"),
        ("subcontracting_limitation_check", "VARCHAR(32)"),
        ("subcontracting_limitation_context", "TEXT"),
        ("subcontracting_limitation_percentage", "NUMERIC(5, 2)"),
        ("far_52219_14_present", "BOOLEAN"),
    ]
    with engine.connect() as conn:
        for name, col_type in columns:
            conn.execute(text(f"ALTER TABLE contracts ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_contracts_subcontracting_limitation_check "
                "ON contracts (subcontracting_limitation_check)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_contracts_far_52219_14_present "
                "ON contracts (far_52219_14_present)"
            )
        )
        conn.commit()


def _migrate_add_contract_margin() -> None:
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS margin_percentage NUMERIC(5, 2)")
        )
        conn.commit()


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


def _migrate_add_submission_package() -> None:
    columns = [
        ("submission_method", "VARCHAR(32)"),
        ("submission_email", "VARCHAR(256)"),
        ("submission_method_confirmed", "BOOLEAN DEFAULT FALSE"),
        ("submission_method_notes", "TEXT"),
        ("pricing_schedule_required", "BOOLEAN DEFAULT FALSE"),
        ("pricing_schedule_attachment_id", "INTEGER"),
        ("multiple_pricing_encouraged", "BOOLEAN DEFAULT FALSE"),
        ("sf1449_required", "BOOLEAN DEFAULT FALSE"),
        ("evaluation_criteria_type", "VARCHAR(32)"),
        ("questions_deadline", "DATE"),
        ("submission_checklist", "JSONB"),
        ("co_questions", "JSONB"),
    ]
    with engine.connect() as conn:
        for name, col_type in columns:
            conn.execute(text(f"ALTER TABLE contracts ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_contracts_submission_method "
                "ON contracts (submission_method)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_contracts_pricing_schedule_required "
                "ON contracts (pricing_schedule_required)"
            )
        )
        conn.commit()

    session = SessionLocal()
    try:
        from models import Contract
        from submission_package import apply_submission_package

        rows = (
            session.query(Contract)
            .filter(Contract.attachment_text.isnot(None), Contract.attachment_text != "")
            .all()
        )
        for row in rows:
            apply_submission_package(row, session)
        session.commit()
    finally:
        session.close()


def _migrate_add_sub_contacts() -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "ALTER TABLE contracts ADD COLUMN IF NOT EXISTS "
                "sub_checklist_bypassed_at TIMESTAMP WITH TIME ZONE"
            )
        )
        conn.commit()
    from sub_contact_service import migrate_contract_subs_to_sub_contacts

    migrate_contract_subs_to_sub_contacts()


def _migrate_add_performance() -> None:
    columns = [
        ("award_date", "DATE"),
        ("period_of_performance_start", "DATE"),
        ("period_of_performance_end", "DATE"),
        ("option_years_remaining", "INTEGER"),
        ("government_contract_number", "VARCHAR(64)"),
        ("invoicing_system", "VARCHAR(32)"),
        ("invoicing_system_confirmed", "BOOLEAN DEFAULT FALSE"),
        ("cor_name", "VARCHAR(256)"),
        ("cor_email", "VARCHAR(256)"),
        ("cor_phone", "VARCHAR(64)"),
        ("co_name", "VARCHAR(256)"),
        ("co_email", "VARCHAR(256)"),
        ("co_phone", "VARCHAR(64)"),
        ("stop_work_issued", "BOOLEAN DEFAULT FALSE"),
        ("stop_work_issued_date", "DATE"),
        ("cpars_rating", "VARCHAR(32)"),
        ("cpars_comments", "TEXT"),
        ("cpars_expected_date", "DATE"),
        ("amendments_last_checked_at", "TIMESTAMP WITH TIME ZONE"),
        ("amendment_alert_active", "BOOLEAN DEFAULT FALSE"),
        ("amendment_alert_data", "JSONB"),
        ("amendments_reviewed_at", "TIMESTAMP WITH TIME ZONE"),
        ("amendment_monitoring_active", "BOOLEAN DEFAULT TRUE"),
    ]
    with engine.connect() as conn:
        for name, col_type in columns:
            conn.execute(text(f"ALTER TABLE contracts ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.commit()
