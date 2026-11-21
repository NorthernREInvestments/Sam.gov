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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS team_status VARCHAR(32) DEFAULT 'active'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_email VARCHAR(255)",
        "ALTER TABLE user_invitations ADD COLUMN IF NOT EXISTS invited_name VARCHAR(255)",
        "ALTER TABLE user_invitations ADD COLUMN IF NOT EXISTS purpose VARCHAR(32) DEFAULT 'new_user'",
        "ALTER TABLE user_invitations ADD COLUMN IF NOT EXISTS target_user_id UUID",
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
        "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS progress JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN DEFAULT FALSE",
        "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS parent_run_id UUID",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception:
                pass


def _backfill_opportunity_titles(db) -> None:
    """One-time soft backfill for display titles + service classification (no deletes)."""
    from m3.models import Opportunity
    from m3.research.classify import classify_requirement
    from m3.web.titles import build_display_title

    rows = db.query(Opportunity).filter(
        (Opportunity.display_title.is_(None)) | (Opportunity.display_title == "")
    ).limit(500).all()
    changed = 0
    for o in rows:
        titles = build_display_title(
            official_title=o.title,
            buyer_name=o.buyer_name,
            solicitation_number=o.solicitation_number,
        )
        o.display_title = titles["display_title"]
        if not o.requirement_class or o.requirement_class == "unknown_mixed":
            clf = classify_requirement(o.title or "", category_raw=o.product_category, procurement_type=o.procurement_type)
            o.requirement_class = clf["requirement_class"]
        db.add(o)
        changed += 1
    if changed:
        db.commit()


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
        try:
            _backfill_opportunity_titles(db)
        except Exception:
            pass
    finally:
        db.close()
