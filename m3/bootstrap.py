"""DB init, owner bootstrap, source seeds."""

from __future__ import annotations

from sqlalchemy import text

from m3.config import get_settings
from m3.db import Base, SessionLocal, get_engine
from m3.research.engine import ensure_source_registry
from m3.services import create_user, find_user_by_email


def _ensure_columns(engine) -> None:
    """Additive migrations for Build 001A/001B without wiping data."""
    stmts = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_primary_owner BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER DEFAULT 1",
        "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS result_snapshot JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS requirement_class VARCHAR(64)",
        "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS quantity VARCHAR(128)",
        "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS data_origin VARCHAR(64) DEFAULT 'operator'",
        "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS display_title VARCHAR(512)",
        "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS business_address TEXT",
        "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS address_type VARCHAR(64)",
        "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS general_phone VARCHAR(64)",
        "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS general_email VARCHAR(255)",
        "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS data_origin VARCHAR(64) DEFAULT 'operator'",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception:
                pass


def init_database() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _ensure_columns(engine)
    db = SessionLocal()
    try:
        ensure_source_registry(db)
        settings = get_settings()
        boot = settings.owner_bootstrap()
        if boot:
            email, password, name = boot
            existing = find_user_by_email(db, email)
            if not existing:
                owner = create_user(db, email=email, full_name=name, password=password, role="owner", actor=None)
                owner.is_primary_owner = True
                db.add(owner)
                db.commit()
            else:
                # Reconcile primary-owner flag for the bootstrap account only
                if not existing.is_primary_owner:
                    existing.is_primary_owner = True
                    existing.role = "owner"
                    db.add(existing)
                    db.commit()
    finally:
        db.close()
