"""Database models for GovTracker."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func
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
    far_52219_14_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    sub_checklist_bypassed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Proposal package / submission
    submission_method: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    submission_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    submission_method_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_method_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    pricing_schedule_required: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    pricing_schedule_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_attachments.id", ondelete="SET NULL"), nullable=True
    )
    multiple_pricing_encouraged: Mapped[bool] = mapped_column(Boolean, default=False)
    sf1449_required: Mapped[bool] = mapped_column(Boolean, default=False)
    evaluation_criteria_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    questions_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    submission_checklist: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    co_questions: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)

    # Post-award performance tracking
    award_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_of_performance_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_of_performance_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_years_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    government_contract_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invoicing_system: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invoicing_system_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    cor_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cor_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cor_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    co_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    co_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    co_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_work_issued: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_work_issued_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cpars_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cpars_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpars_expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amendments_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    amendment_alert_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    amendment_alert_data: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    amendments_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    amendment_monitoring_active: Mapped[bool] = mapped_column(Boolean, default=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract_subs: Mapped[list["ContractSub"]] = relationship(
        "ContractSub", back_populates="contract", cascade="all, delete-orphan"
    )
    sub_contacts: Mapped[list["SubContact"]] = relationship(
        "SubContact", back_populates="contract", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["ContractAttachment"]] = relationship(
        "ContractAttachment",
        back_populates="contract",
        cascade="all, delete-orphan",
        foreign_keys="ContractAttachment.contract_id",
    )
    invoices: Mapped[list["ContractInvoice"]] = relationship(
        "ContractInvoice", back_populates="contract", cascade="all, delete-orphan"
    )
    sub_payments: Mapped[list["SubPayment"]] = relationship(
        "SubPayment", back_populates="contract", cascade="all, delete-orphan"
    )


class ContractInvoice(Base):
    __tablename__ = "contract_invoices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    invoice_number: Mapped[str] = mapped_column(String(64), index=True)
    billing_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    billing_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    invoice_submitted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_submission_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invoice_accepted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    days_to_payment: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="Not Started", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship("Contract", back_populates="invoices")
    sub_payments: Mapped[list["SubPayment"]] = relationship("SubPayment", back_populates="invoice")


class SubPayment(Base):
    __tablename__ = "sub_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sub_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("sub_contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sub_invoice_received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sub_invoice_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    government_signoff_received: Mapped[bool] = mapped_column(Boolean, default=False)
    government_signoff_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    government_signoff_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_released_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="Pending Signoff", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship("Contract", back_populates="sub_payments")
    invoice: Mapped["ContractInvoice | None"] = relationship("ContractInvoice", back_populates="sub_payments")
    sub_contact: Mapped["SubContact | None"] = relationship("SubContact", backref="sub_payments")


class ContractAttachment(Base):
    """Persisted solicitation file bytes (PDF and other downloads) — not just URLs."""

    __tablename__ = "contract_attachments"
    __table_args__ = (UniqueConstraint("contract_id", "filename_key", name="uq_contract_attachment_file"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    filename_key: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32), default="sam")  # sam | piee
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_type: Mapped[str] = mapped_column(String(128), default="application/pdf")
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship(
        "Contract", back_populates="attachments", foreign_keys=[contract_id]
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


class SubContact(Base):
    """Per-contract subcontractor outreach, quotes, and selection workflow."""

    __tablename__ = "sub_contacts"
    __table_args__ = (UniqueConstraint("contract_id", "sub_id", name="uq_sub_contact_contract_sub"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int | None] = mapped_column(ForeignKey("subs.id", ondelete="SET NULL"), nullable=True, index=True)
    contract_sub_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_subs.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
    )

    company_name: Mapped[str] = mapped_column(String(512))
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    distance_miles: Mapped[Decimal | None] = mapped_column(Numeric(8, 1), nullable=True)

    called: Mapped[bool] = mapped_column(Boolean, default=False)
    call_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reached: Mapped[bool] = mapped_column(Boolean, default=False)
    voicemail_left: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quote_received: Mapped[bool] = mapped_column(Boolean, default=False)
    quote_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    quote_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payment_terms_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    insurance_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    insurance_expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    insurance_coverage_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    references_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    references_received: Mapped[bool] = mapped_column(Boolean, default=False)
    references_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)

    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="Not Contacted", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    claude_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claude_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", back_populates="sub_contacts")
    sub: Mapped["Sub | None"] = relationship("Sub", backref="sub_contacts")
    contract_sub: Mapped["ContractSub | None"] = relationship("ContractSub", backref="sub_contact")


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
