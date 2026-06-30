"""Database models for GovTracker."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    set_aside: Mapped[str | None] = mapped_column(String(256), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    estimated_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    sam_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pricing_intel: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    selected_sub_quote: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    margin_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    sub_search_status: Mapped[str | None] = mapped_column(String(32), nullable=True, default="none")
    sub_search_radius_miles: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # PWS / internal pricing database fields
    square_footage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cleaning_frequency_per_week: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    special_requirements: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    wage_determination_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wage_determination_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    awarded_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    price_per_sqft_per_year: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    price_per_sqft_per_visit: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    pricing_region: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    # Persisted solicitation PDF text + FAR 52.219-14 compliance
    attachment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_extraction_method: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    attachment_extraction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_text_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subcontracting_limitation_check: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    subcontracting_limitation_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    subcontracting_limitation_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract_subs: Mapped[list["ContractSub"]] = relationship(
        "ContractSub", back_populates="contract", cascade="all, delete-orphan"
    )


class Sub(Base):
    __tablename__ = "subs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    place_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    business_name: Mapped[str] = mapped_column(String(512))
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    zip: Mapped[str | None] = mapped_column(String(16), nullable=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    google_maps_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sub_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    owner_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    insurance_carrier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    business_email: Mapped[str | None] = mapped_column(String(256), nullable=True)

    date_first_found: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    date_last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract_links: Mapped[list["ContractSub"]] = relationship("ContractSub", back_populates="sub")


class ContractSub(Base):
    __tablename__ = "contract_subs"
    __table_args__ = (UniqueConstraint("contract_id", "sub_id", name="uq_contract_sub"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("subs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(64), default="Not Contacted")
    quote_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    quote_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contact_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claude_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    distance_miles: Mapped[Decimal | None] = mapped_column(Numeric(8, 1), nullable=True)
    agreement_signature_status: Mapped[str] = mapped_column(
        String(64), default="Agreement Not Generated", index=True
    )
    agreement_status_log: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    date_status_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    date_added: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship("Contract", back_populates="contract_subs")
    sub: Mapped["Sub"] = relationship("Sub", back_populates="contract_links")
    agreements: Mapped[list["SubcontractAgreement"]] = relationship(
        "SubcontractAgreement", back_populates="contract_sub", cascade="all, delete-orphan"
    )


class SubcontractAgreement(Base):
    __tablename__ = "subcontract_agreements"
    __table_args__ = (UniqueConstraint("contract_sub_id", name="uq_subcontract_agreement_link"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("subs.id", ondelete="CASCADE"), index=True)
    contract_sub_id: Mapped[int] = mapped_column(
        ForeignKey("contract_subs.id", ondelete="CASCADE"), index=True
    )
    agreement_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    date_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="subcontract_agreements")
    sub: Mapped["Sub"] = relationship("Sub", backref="subcontract_agreements")
    contract_sub: Mapped["ContractSub"] = relationship("ContractSub", back_populates="agreements")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int | None] = mapped_column(ForeignKey("subs.id", ondelete="SET NULL"), nullable=True)
    contract_sub_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_subs.id", ondelete="SET NULL"), nullable=True
    )

    sub_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sub_quote: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    margin_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    base_year_bid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    option_year_1: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    option_year_2: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    option_year_3: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    option_year_4: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_all_years: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    option_year_increase_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    proposal_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    sections_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    version_history: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    winning_bid_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    contracting_officer_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    submission_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submission_deadline: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_fields: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    date_created: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    date_submitted: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="proposals")
