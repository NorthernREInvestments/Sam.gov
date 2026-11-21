"""DB init, owner bootstrap, source seeds."""

from __future__ import annotations

from m3.config import get_settings
from m3.db import Base, SessionLocal, get_engine
from m3.models import User
from m3.research.engine import ensure_source_registry
from m3.services import create_user, find_user_by_email


def init_database() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_source_registry(db)
        settings = get_settings()
        boot = settings.owner_bootstrap()
        if boot:
            email, password, name = boot
            if not find_user_by_email(db, email):
                create_user(db, email=email, full_name=name, password=password, role="owner", actor=None)
        # Ensure a supplier_manager exists for demos if owner set SAM-style secondary?
        # Do not invent passwords — owner creates users in Settings.
    finally:
        db.close()
