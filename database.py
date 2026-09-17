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
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=10,
    max_overflow=20,
    connect_args={"connect_timeout": 15},
    pool_timeout=30,
)
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


def is_transient_db_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            "server closed the connection",
            "connection reset",
            "connection timed out",
            "could not connect",
            "ssl syscall error",
            "connection already closed",
        )
    )


def reset_connection_pool() -> None:
    """Drop stale pooled connections after idle timeouts (common on Railway Postgres)."""
    engine.dispose()


def with_db_retry(fn, *, attempts: int = 3, base_delay: float = 1.0):
    """Retry DB work after transient connection drops (e.g. during long AI calls)."""
    import time

    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except (OperationalError, SQLAlchemyError) as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not is_transient_db_error(exc):
                raise
            reset_connection_pool()
            time.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_db_retry exhausted without result")


def init_db() -> None:
    import logging

    log = logging.getLogger("govtracker.db")
    from models import (  # noqa: F401
        AiAnalysisJob,
        AppSetting,
        AskAboutDealRequest,
        AttachmentQueueItem,
        AwardLifecycle,
        BidPackage,
        CallTranscript,
        CompanyCapability,
        Contract,
        ContractAttachment,
        ContractInvoice,
        ContractSub,
        CrmActivity,
        CsvOpportunity,
        DealState,
        DealWarning,
        DiscoveredOpportunity,
        DiscoveryAgency,
        DiscoveryRun,
        DiscoverySource,
        FinancierContact,
        FinancingProvider,
        FinancingPursuit,
        FinancingTerm,
        FreightQuote,
        FundingGap,
        FundingPlan,
        FundingStrategyCandidate,
        GovernmentContact,
        KnowledgeProduct,
        KnowledgeSupplier,
        MissingInfoItem,
        OperatorNote,
        OpportunitySighting,
        ActionQueueItem,
        ProductRequirement,
        Proposal,
        QuoteDocument,
        RequirementRegisterItem,
        ResearchEvent,
        SamApiAudit,
        SolicitationDocument,
        Sub,
        SubContact,
        SubPayment,
        SubcontractAgreement,
        SupplierContact,
        SupplierOffer,
        SupplierPursuitPlan,
    )

    log.info("init_db: rename legacy tables")
    print("govtracker: init_db rename legacy tables", flush=True)
    _migrate_rename_tables_to_gt_prefix()
    log.info("init_db: schema")
    print("govtracker: init_db schema", flush=True)
    Base.metadata.create_all(bind=engine)
    log.info("init_db: migrations")
    print("govtracker: init_db migrations", flush=True)
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
    _migrate_add_csv_attachment_queue()
    _migrate_add_csv_opportunity_pricing()
    _migrate_add_deal_workspace_columns()
    _migrate_add_govcon_os_foundation()
    _migrate_add_live_discovery_coverage()
    log.info("init_db: done")
    print("govtracker: init_db done", flush=True)


def _migrate_rename_tables_to_gt_prefix() -> None:
    """One-time rename of GovTracker tables when sharing Postgres with other apps."""
    from db_tables import GT_TABLE_RENAMES

    with engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        renamed = 0
        for old_name, new_name in GT_TABLE_RENAMES:
            if old_name in existing and new_name not in existing:
                conn.execute(text(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'))
                existing.discard(old_name)
                existing.add(new_name)
                renamed += 1
        conn.commit()
        if renamed:
            import logging

            logging.getLogger("govtracker.db").info(
                "Renamed %s legacy table(s) to gt_ prefix", renamed
            )


def _migrate_add_attachment_compliance() -> None:
    from db_tables import GT_CONTRACTS

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
            conn.execute(text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_gt_contracts_subcontracting_limitation_check "
                f"ON {GT_CONTRACTS} (subcontracting_limitation_check)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_gt_contracts_far_52219_14_present "
                f"ON {GT_CONTRACTS} (far_52219_14_present)"
            )
        )
        conn.commit()


def _migrate_add_contract_margin() -> None:
    from db_tables import GT_CONTRACTS

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS margin_percentage NUMERIC(5, 2)")
        )
        conn.commit()


def _migrate_add_sub_agreements() -> None:
    from db_tables import GT_CONTRACT_SUBS, GT_SUBS
    sub_columns = [
        ("owner_name", "VARCHAR(256)"),
        ("owner_title", "VARCHAR(128)"),
        ("license_number", "VARCHAR(128)"),
        ("insurance_carrier", "VARCHAR(256)"),
        ("business_email", "VARCHAR(256)"),
    ]
    with engine.connect() as conn:
        for name, col_type in sub_columns:
            conn.execute(text(f"ALTER TABLE {GT_SUBS} ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                f"ALTER TABLE {GT_CONTRACT_SUBS} ADD COLUMN IF NOT EXISTS "
                "agreement_signature_status VARCHAR(64) DEFAULT 'Agreement Not Generated'"
            )
        )
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACT_SUBS} ADD COLUMN IF NOT EXISTS agreement_status_log JSONB")
        )
        conn.commit()


def _migrate_add_contract_tier() -> None:
    from db_tables import GT_CONTRACTS
    from naics_labels import NAICS_TIER_BY_CODE

    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS tier INTEGER"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_gt_contracts_tier ON {GT_CONTRACTS} (tier)"))
        for code, tier in NAICS_TIER_BY_CODE.items():
            conn.execute(
                text(f"UPDATE {GT_CONTRACTS} SET tier = :tier WHERE naics_code = :code AND tier IS NULL"),
                {"tier": tier, "code": code},
            )
        conn.commit()


def _migrate_add_proposals() -> None:
    """Proposals table created via create_all; no-op migration hook for future alters."""
    pass


def _migrate_add_internal_pricing() -> None:
    from db_tables import GT_CONTRACTS

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
            conn.execute(text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.commit()


def _migrate_add_sub_finder() -> None:
    from db_tables import GT_CONTRACTS

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS selected_sub_quote NUMERIC(14, 2)")
        )
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS sub_search_status VARCHAR(32)")
        )
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS sub_search_radius_miles INTEGER")
        )
        conn.commit()


def _migrate_add_sam_raw() -> None:
    from db_tables import GT_CONTRACTS

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS sam_raw JSONB")
        )
        conn.commit()


def _migrate_add_pricing_intel() -> None:
    from db_tables import GT_CONTRACTS

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS pricing_intel JSONB")
        )
        conn.commit()


def _migrate_add_submission_package() -> None:
    from db_tables import GT_CONTRACTS

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
            conn.execute(text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_gt_contracts_submission_method "
                f"ON {GT_CONTRACTS} (submission_method)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_gt_contracts_pricing_schedule_required "
                f"ON {GT_CONTRACTS} (pricing_schedule_required)"
            )
        )
        conn.commit()


def _migrate_add_sub_contacts() -> None:
    from db_tables import GT_CONTRACTS

    with engine.connect() as conn:
        conn.execute(
            text(
                f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS "
                "sub_checklist_bypassed_at TIMESTAMP WITH TIME ZONE"
            )
        )
        conn.commit()
    from sub_contact_service import migrate_contract_subs_to_sub_contacts

    migrate_contract_subs_to_sub_contacts()


def _migrate_add_performance() -> None:
    from db_tables import GT_CONTRACTS

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
            conn.execute(text(f"ALTER TABLE {GT_CONTRACTS} ADD COLUMN IF NOT EXISTS {name} {col_type}"))
        conn.commit()


def _migrate_add_csv_attachment_queue() -> None:
    from db_tables import GT_ATTACHMENT_QUEUE

    with engine.connect() as conn:
        conn.execute(
            text(
                f"ALTER TABLE {GT_ATTACHMENT_QUEUE} "
                "ADD COLUMN IF NOT EXISTS watchlist_confidence VARCHAR(16)"
            )
        )
        conn.execute(
            text(f"UPDATE {GT_ATTACHMENT_QUEUE} SET status = 'queued' WHERE status = 'pending'")
        )
        conn.execute(
            text(
                f"UPDATE {GT_ATTACHMENT_QUEUE} SET status = 'downloading' WHERE status = 'processing'"
            )
        )
        conn.commit()


def _migrate_add_csv_opportunity_pricing() -> None:
    from db_tables import GT_CSV_OPPORTUNITIES

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_CSV_OPPORTUNITIES} ADD COLUMN IF NOT EXISTS pricing_intel JSONB")
        )
        conn.commit()


def _migrate_add_deal_workspace_columns() -> None:
    """Additive columns for Deal Workspace CRM on existing knowledge tables."""
    from db_tables import GT_KNOWLEDGE_SUPPLIERS, GT_SUPPLIER_OFFERS

    supplier_cols = [
        ("federal_capability", "VARCHAR(64)"),
        ("relationship_strength", "VARCHAR(64)"),
        ("last_contact_at", "TIMESTAMP WITH TIME ZONE"),
        ("next_followup_at", "TIMESTAMP WITH TIME ZONE"),
        ("crm_json", "JSONB"),
    ]
    offer_cols = [
        ("quote_number", "VARCHAR(128)"),
        ("validation_status", "VARCHAR(32)"),
        ("bom_match", "BOOLEAN"),
        ("quote_details_json", "JSONB"),
    ]
    with engine.connect() as conn:
        for name, col_type in supplier_cols:
            conn.execute(
                text(f"ALTER TABLE {GT_KNOWLEDGE_SUPPLIERS} ADD COLUMN IF NOT EXISTS {name} {col_type}")
            )
        for name, col_type in offer_cols:
            conn.execute(
                text(f"ALTER TABLE {GT_SUPPLIER_OFFERS} ADD COLUMN IF NOT EXISTS {name} {col_type}")
            )
        conn.commit()


def _migrate_add_govcon_os_foundation() -> None:
    """Additive columns/tables for GovCon OS foundation build."""
    from db_tables import GT_CALL_TRANSCRIPTS, GT_FINANCING_PROVIDERS

    with engine.connect() as conn:
        conn.execute(
            text(f"ALTER TABLE {GT_FINANCING_PROVIDERS} ADD COLUMN IF NOT EXISTS profile_json JSONB")
        )
        transcript_cols = [
            ("provider_id", "INTEGER"),
            ("transcript_type", "VARCHAR(32)"),
            ("organization_name", "VARCHAR(512)"),
            ("operator", "VARCHAR(128)"),
            ("analysis_status", "VARCHAR(64)"),
        ]
        for name, col_type in transcript_cols:
            conn.execute(
                text(f"ALTER TABLE {GT_CALL_TRANSCRIPTS} ADD COLUMN IF NOT EXISTS {name} {col_type}")
            )
        conn.commit()


def _migrate_add_live_discovery_coverage() -> None:
    """Agency live_capable / last_verified + source live validation evidence."""
    from db_tables import GT_DISCOVERY_AGENCIES, GT_DISCOVERY_SOURCES

    with engine.connect() as conn:
        conn.execute(
            text(
                f"ALTER TABLE {GT_DISCOVERY_AGENCIES} "
                f"ADD COLUMN IF NOT EXISTS live_capable BOOLEAN DEFAULT FALSE"
            )
        )
        conn.execute(
            text(
                f"ALTER TABLE {GT_DISCOVERY_AGENCIES} "
                f"ADD COLUMN IF NOT EXISTS last_verified VARCHAR(32)"
            )
        )
        for name, col_type in [
            ("last_live_verified_at", "TIMESTAMPTZ"),
            ("last_live_validation_result", "VARCHAR(64)"),
            ("last_live_http_status", "INTEGER"),
            ("last_live_record_count", "INTEGER"),
            ("last_live_sample_external_id", "VARCHAR(256)"),
            ("validation_notes", "TEXT"),
        ]:
            conn.execute(
                text(
                    f"ALTER TABLE {GT_DISCOVERY_SOURCES} "
                    f"ADD COLUMN IF NOT EXISTS {name} {col_type}"
                )
            )
        conn.commit()
