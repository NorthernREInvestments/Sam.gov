"""Database models for GovTracker."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AppSetting(Base):
    """Key-value store for sync rotation and other app state."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    notice_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    agency: Mapped[str | None] = mapped_column(String(512), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    naics_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    set_aside: Mapped[str | None] = mapped_column(String(256), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    estimated_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    sam_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
