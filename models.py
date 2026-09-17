"""Database models for GovTracker."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class AppSetting(Base):
    """Key-value store for sync rotation and other app state."""

    __tablename__ = "gt_app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Contract(Base):
    __tablename__ = "gt_contracts"

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
        ForeignKey("gt_contract_attachments.id", ondelete="SET NULL"), nullable=True
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
    __tablename__ = "gt_contract_invoices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
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
    __tablename__ = "gt_sub_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contract_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sub_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_sub_contacts.id", ondelete="SET NULL"), nullable=True, index=True
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

    __tablename__ = "gt_contract_attachments"
    __table_args__ = (UniqueConstraint("contract_id", "filename_key", name="uq_contract_attachment_file"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
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
    __tablename__ = "gt_subs"

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

    __tablename__ = "gt_sub_contacts"
    __table_args__ = (UniqueConstraint("contract_id", "sub_id", name="uq_sub_contact_contract_sub"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int | None] = mapped_column(ForeignKey("gt_subs.id", ondelete="SET NULL"), nullable=True, index=True)
    contract_sub_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contract_subs.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
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
    __tablename__ = "gt_contract_subs"
    __table_args__ = (UniqueConstraint("contract_id", "sub_id", name="uq_contract_sub"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("gt_subs.id", ondelete="CASCADE"), index=True)
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
    __tablename__ = "gt_subcontract_agreements"
    __table_args__ = (UniqueConstraint("contract_sub_id", name="uq_subcontract_agreement_link"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int] = mapped_column(ForeignKey("gt_subs.id", ondelete="CASCADE"), index=True)
    contract_sub_id: Mapped[int] = mapped_column(
        ForeignKey("gt_contract_subs.id", ondelete="CASCADE"), index=True
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
    __tablename__ = "gt_proposals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[int | None] = mapped_column(ForeignKey("gt_subs.id", ondelete="SET NULL"), nullable=True)
    contract_sub_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contract_subs.id", ondelete="SET NULL"), nullable=True
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


class CsvOpportunity(Base):
    """SAM.gov ContractOpportunitiesFullCSV import rows (GovTracker-owned gt_ table)."""

    __tablename__ = "gt_csv_opportunities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    notice_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    solicitation_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agency: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contracting_office: Mapped[str | None] = mapped_column(String(512), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    naics_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    set_aside: Mapped[str | None] = mapped_column(String(256), nullable=True)
    location_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    co_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    co_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    co_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sam_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="New", index=True)
    watchlist_match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watchlist_match_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    watchlist_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    sam_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pricing_intel: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract | None"] = relationship("Contract", foreign_keys=[contract_id])
    queue_items: Mapped[list["AttachmentQueueItem"]] = relationship(
        "AttachmentQueueItem", back_populates="csv_opportunity", cascade="all, delete-orphan"
    )


class AttachmentQueueItem(Base):
    """Pending SAM.gov attachment fetches for CSV-imported opportunities."""

    __tablename__ = "gt_attachment_queue"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    csv_opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("gt_csv_opportunities.id", ondelete="CASCADE"), index=True
    )
    notice_id: Mapped[str] = mapped_column(String(128), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    watchlist_match: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    watchlist_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    sam_api_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    csv_opportunity: Mapped["CsvOpportunity"] = relationship(
        "CsvOpportunity", back_populates="queue_items"
    )


# ---------------------------------------------------------------------------
# Product-resale knowledge store (GovCon namespace — NOT sibling `products`)
# ---------------------------------------------------------------------------


class ProductRequirement(Base):
    """Durable validated product requirement fields linked to an opportunity."""

    __tablename__ = "gt_product_requirements"
    __table_args__ = (
        UniqueConstraint("contract_id", "field_key", name="uq_gt_product_req_field"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    notice_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    solicitation_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    field_key: Mapped[str] = mapped_column(String(128), index=True)
    field_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value_json: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    source_document_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="product_requirements")


class KnowledgeProduct(Base):
    """Reusable product identity for GovCon product resale (not sibling products table)."""

    __tablename__ = "gt_knowledge_products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    manufacturer: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    part_number: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upc_gtin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    specifications_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    country_of_origin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeSupplier(Base):
    """Reusable supplier identity — Places businesses are NOT auto-verified."""

    __tablename__ = "gt_knowledge_suppliers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(512), index=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    manufacturer_relationship: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CRM enrichment
    federal_capability: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relationship_strength: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crm_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierOffer(Base):
    """Supplier offer/quote — CURRENT vs HISTORICAL must stay explicit."""

    __tablename__ = "gt_supplier_offers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    quantity_basis: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    extended_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    extended_price_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    availability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stock_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lead_time: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shipping_included: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    quote_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(64), default="OTHER", index=True
    )  # FORMAL_QUOTE | CURRENT_LISTED_PRICE | CATALOG_PRICE | HISTORICAL_PRICE | OTHER
    temporal_class: Mapped[str] = mapped_column(
        String(32), default="HISTORICAL", index=True
    )  # CURRENT | HISTORICAL
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deal Workspace quote enrichment (optional; NULL-safe for legacy rows)
    quote_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    bom_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    quote_details_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped["KnowledgeProduct | None"] = relationship("KnowledgeProduct", backref="offers")
    supplier: Mapped["KnowledgeSupplier | None"] = relationship("KnowledgeSupplier", backref="offers")
    contract: Mapped["Contract | None"] = relationship("Contract", backref="supplier_offers")


class FreightQuote(Base):
    __tablename__ = "gt_freight_quotes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    origin: Mapped[str | None] = mapped_column(String(512), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quantity_basis: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    quote_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    temporal_class: Mapped[str] = mapped_column(String(32), default="HISTORICAL", index=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FinancingProvider(Base):
    """Reusable financing provider facts only — not deal approval."""

    __tablename__ = "gt_financing_providers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(512), index=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    product_types: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FinancingTerm(Base):
    """Deal-specific financing terms — marketing language never auto-approves."""

    __tablename__ = "gt_financing_terms"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_financing_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    financed_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    financed_amount_basis: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    duration: Mapped[str | None] = mapped_column(String(128), nullable=True)
    advance_structure: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_payment_mechanics: Mapped[str | None] = mapped_column(Text, nullable=True)
    government_payment_mechanics: Mapped[str | None] = mapped_column(Text, nullable=True)
    pg_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    personal_credit_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cash_deposit_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    eligibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    temporal_class: Mapped[str] = mapped_column(String(32), default="HISTORICAL", index=True)
    is_generic_marketing: Mapped[bool] = mapped_column(Boolean, default=False)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    provider: Mapped["FinancingProvider | None"] = relationship(
        "FinancingProvider", backref="terms"
    )
    contract: Mapped["Contract | None"] = relationship("Contract", backref="financing_terms")


class SamApiAudit(Base):
    """Persistent audit of every SAM API gate decision / call attempt."""

    __tablename__ = "gt_sam_api_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(128), index=True)
    eligibility_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    eligibility_reasons_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    sam_api_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    fact_to_verify: Mapped[str | None] = mapped_column(String(512), nullable=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    useful_new_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract | None"] = relationship("Contract", backref="sam_api_audits")


class ResearchEvent(Base):
    """Auditable research event — what/why/source/cost/reuse."""

    __tablename__ = "gt_research_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_code: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    result_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    facts_produced_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    reusable: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    external_api_calls: Mapped[int] = mapped_column(Integer, default=0)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract | None"] = relationship("Contract", backref="research_events")


class DealState(Base):
    """Persisted product-deal decision/economics snapshot for an opportunity."""

    __tablename__ = "gt_deal_states"
    __table_args__ = (UniqueConstraint("contract_id", name="uq_gt_deal_state_contract"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    core_fit: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    pipeline_stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    reason_codes_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    economics_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    deal_score_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    portfolio_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    financing_gate_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    research_plan_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    operator_bid_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    match_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    funnel_checkpoint_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ai_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship("Contract", backref="deal_state")


# --- Deal Workspace / CRM foundation -----------------------------------------


class CompanyCapability(Base):
    """Editable company capability / policy fact — UNKNOWN never means held."""

    __tablename__ = "gt_company_capabilities"
    __table_args__ = (UniqueConstraint("capability_key", name="uq_gt_company_cap_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    capability_key: Mapped[str] = mapped_column(String(128), index=True)
    value_json: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    held: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SolicitationDocument(Base):
    """Inventory of solicitation package documents for an opportunity."""

    __tablename__ = "gt_solicitation_documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    document_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version_amendment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text_extraction_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    required_for_bid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    local_attachment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract"] = relationship("Contract", backref="solicitation_documents")


class RequirementRegisterItem(Base):
    """Actionable solicitation requirement register row."""

    __tablename__ = "gt_requirement_register"
    __table_args__ = (
        UniqueConstraint("contract_id", "requirement_key", name="uq_gt_req_register_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    requirement_key: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text)
    requirement_type: Mapped[str] = mapped_column(String(64), index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    source_document: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    satisfied_by: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="requirement_register")


class SupplierContact(Base):
    """CRM contact tied to a knowledge supplier."""

    __tablename__ = "gt_supplier_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    title_role: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_federal_team: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    supplier: Mapped["KnowledgeSupplier | None"] = relationship(
        "KnowledgeSupplier", backref="contacts"
    )


class CrmActivity(Base):
    """CRM activity log — operator-reported facts stay OPERATOR_REPORTED until written evidence."""

    __tablename__ = "gt_crm_activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_supplier_contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    activity_type: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_learned_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    documents_received_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    operator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contract: Mapped["Contract | None"] = relationship("Contract", backref="crm_activities")


class SupplierPursuitPlan(Base):
    """Per-opportunity supplier pursuit / negotiation plan."""

    __tablename__ = "gt_supplier_pursuit_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    contact_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_bom_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(512), nullable=True)
    delivery_deadline: Mapped[str | None] = mapped_column(String(256), nullable=True)
    authorization_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    compliance_requirements_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    questions_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    quote_requirements_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    negotiation_status: Mapped[str] = mapped_column(String(64), default="NOT_STARTED")
    negotiation_suggestions_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_request_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="supplier_pursuit_plans")
    supplier: Mapped["KnowledgeSupplier | None"] = relationship(
        "KnowledgeSupplier", backref="pursuit_plans"
    )


class FinancingPursuit(Base):
    """Financing CRM pursuit tied to an opportunity + provider."""

    __tablename__ = "gt_financing_pursuits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_financing_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    transaction_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    pg_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    personal_credit_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    borrower_cash_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supplier_direct_payment: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    advance_percentage: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    minimum_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    maximum_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    fees: Mapped[str | None] = mapped_column(Text, nullable=True)
    recourse: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(64), default="UNRESOLVED", index=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    next_followup: Mapped[date | None] = mapped_column(Date, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract | None"] = relationship("Contract", backref="financing_pursuits")
    provider: Mapped["FinancingProvider | None"] = relationship(
        "FinancingProvider", backref="pursuits"
    )


class BidPackage(Base):
    """Persisted bid package skeleton for an opportunity (no AI auto-write)."""

    __tablename__ = "gt_bid_packages"
    __table_args__ = (UniqueConstraint("contract_id", name="uq_gt_bid_package_contract"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    package_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    readiness_status: Mapped[str] = mapped_column(String(32), default="BID_NOT_READY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="bid_package")


class MissingInfoItem(Base):
    """Missing-information / CO clarification workflow item with search audit."""

    __tablename__ = "gt_missing_info_items"
    __table_args__ = (
        UniqueConstraint("contract_id", "fact_key", name="uq_gt_missing_info_fact"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    fact_key: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_class: Mapped[str] = mapped_column(String(64), default="SOLICITATION_FACT", index=True)
    status: Mapped[str] = mapped_column(String(64), default="MISSING_UNCHECKED", index=True)
    necessary_for_bid: Mapped[bool] = mapped_column(Boolean, default=True)
    necessary_for_execution: Mapped[bool] = mapped_column(Boolean, default=True)
    search_audit_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    co_gate_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    question_draft_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    second_pass_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_to_ask_co: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_absent: Mapped[str | None] = mapped_column(String(16), nullable=True)
    already_answered: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="missing_info_items")


class GovernmentContact(Base):
    """Contracting Officer / government contact CRM."""

    __tablename__ = "gt_government_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    agency: Mapped[str | None] = mapped_column(String(512), nullable=True)
    office: Mapped[str | None] = mapped_column(String(256), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact_provenance: Mapped[str] = mapped_column(String(64), default="OPERATOR_REPORTED")
    documented_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    clarification_history_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract | None"] = relationship("Contract", backref="government_contacts")


class OperatorNote(Base):
    """Searchable freeform notes — OPERATOR_REPORTED by default."""

    __tablename__ = "gt_operator_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance: Mapped[str] = mapped_column(String(64), default="OPERATOR_REPORTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CallTranscript(Base):
    """Quo-ready call transcript store — facts never auto-VERIFIED."""

    __tablename__ = "gt_call_transcripts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_supplier_contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    government_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_government_contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_financing_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transcript_type: Mapped[str] = mapped_column(String(32), default="SUPPLIER", index=True)
    organization_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(64), default="NOT_ANALYZED", index=True)
    source: Mapped[str] = mapped_column(String(64), default="MANUAL")  # MANUAL | QUO_CALL_IMPORTED
    external_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    transcript_status: Mapped[str] = mapped_column(String(64), default="TRANSCRIPT_RAW", index=True)
    raw_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_extraction_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    operator_confirmed_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActionQueueItem(Base):
    """Persisted operator Today / action queue item."""

    __tablename__ = "gt_action_queue_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    action: Mapped[str] = mapped_column(Text)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    due_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QuoteDocument(Base):
    """Supplier quote document attachment foundation — no auto AI verification."""

    __tablename__ = "gt_quote_documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_knowledge_suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_supplier_offers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    quote_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    verification_state: Mapped[str] = mapped_column(
        String(64), default="QUOTE_DOCUMENT_RECEIVED", index=True
    )
    extraction_status: Mapped[str] = mapped_column(String(64), default="EXTRACTION_PENDING")
    extraction_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FundingPlan(Base):
    """Durable pre-bid / award funding plan per opportunity."""

    __tablename__ = "gt_funding_plans"
    __table_args__ = (UniqueConstraint("contract_id", name="uq_gt_funding_plan_contract"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    pre_bid_status: Mapped[str] = mapped_column(String(64), default="NOT_RESEARCHED", index=True)
    award_confirmation_status: Mapped[str] = mapped_column(String(64), default="PENDING", index=True)
    capital_required: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    capital_required_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    capital_timing: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_structure: Mapped[str | None] = mapped_column(String(256), nullable=True)
    personal_cash_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    personal_credit_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pg_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supplier_payment_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    freight_funding_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    government_payment_assumption: Mapped[str | None] = mapped_column(Text, nullable=True)
    funding_cost_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    unresolved_gaps_json: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cash_cycle_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    economics_scenarios_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="funding_plan_row")


class FundingStrategyCandidate(Base):
    __tablename__ = "gt_funding_strategy_candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    funding_plan_id: Mapped[int] = mapped_column(ForeignKey("gt_funding_plans.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="POSSIBLE_STRATEGY")
    rank_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_fact: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    funding_plan: Mapped["FundingPlan"] = relationship("FundingPlan", backref="strategy_candidates")


class FundingGap(Base):
    __tablename__ = "gt_funding_gaps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    funding_plan_id: Mapped[int] = mapped_column(ForeignKey("gt_funding_plans.id", ondelete="CASCADE"), index=True)
    gap_key: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    must_know_before_bid: Mapped[bool] = mapped_column(Boolean, default=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    funding_plan: Mapped["FundingPlan"] = relationship("FundingPlan", backref="gaps")


class DealWarning(Base):
    """Deterministic deal exception / YOU MISSED THIS warnings."""

    __tablename__ = "gt_deal_warnings"
    __table_args__ = (
        UniqueConstraint("contract_id", "warning_type", "active_key", name="uq_gt_deal_warning_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    warning_type: Mapped[str] = mapped_column(String(128), index=True)
    active_key: Mapped[str] = mapped_column(String(256), default="default")
    severity: Mapped[str] = mapped_column(String(32), default="MEDIUM", index=True)
    message: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recommended_next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    contract: Mapped["Contract"] = relationship("Contract", backref="deal_warnings")


class AwardLifecycle(Base):
    __tablename__ = "gt_award_lifecycles"
    __table_args__ = (UniqueConstraint("contract_id", name="uq_gt_award_lifecycle_contract"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(64), default="SUBMITTED", index=True)
    submission_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_method: Mapped[str | None] = mapped_column(String(256), nullable=True)
    submission_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    award_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    award_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    award_po_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    award_document_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    funding_final_approval: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supplier_order_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shipment_tracking: Mapped[str | None] = mapped_column(String(512), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    government_acceptance: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    government_payment_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    financier_payoff_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company_proceeds_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    milestones_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contract: Mapped["Contract"] = relationship("Contract", backref="award_lifecycle_row")


class FinancierContact(Base):
    __tablename__ = "gt_financier_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_financing_providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    title_role: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="OPERATOR_REPORTED")
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AskAboutDealRequest(Base):
    __tablename__ = "gt_ask_about_deal_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("gt_contracts.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_preview_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="PREVIEW_ONLY", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiAnalysisJob(Base):
    """Future event-driven AI work queue — no auto execution in this build."""

    __tablename__ = "gt_ai_analysis_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_event: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="PENDING", index=True)
    operating_mode: Mapped[str] = mapped_column(String(32), default="LEAN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscoverySource(Base):
    """Source registry for non-SAM broad discovery network."""

    __tablename__ = "gt_discovery_sources"
    __table_args__ = (UniqueConstraint("source_id", name="uq_gt_discovery_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    source_name: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    platform_family: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    discovery_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    list_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    detail_url_pattern: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    adapter_status: Mapped[str] = mapped_column(String(32), default="PLANNED", index=True)
    trust_tier: Mapped[int] = mapped_column(Integer, default=3)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    health_status: Mapped[str] = mapped_column(String(32), default="UNTESTED", index=True)
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dedup_keys_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    records_seen: Mapped[int] = mapped_column(Integer, default=0)
    records_added: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    parse_warning_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_live_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_live_validation_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_live_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_live_record_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_live_sample_external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    validation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryAgency(Base):
    """Agency registry — one platform adapter can serve many agencies."""

    __tablename__ = "gt_discovery_agencies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agency_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512))
    buyer_type: Mapped[str] = mapped_column(String(64), index=True)
    state_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(256), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(128), nullable=True)
    procurement_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    platform_family: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    platform_detection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    live_capable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_verified: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscoveredOpportunity(Base):
    """Canonical discovered opportunity — source-agnostic, feeds deal engine."""

    __tablename__ = "gt_discovered_opportunities"
    __table_args__ = (
        UniqueConstraint("canonical_key", name="uq_gt_discovered_opp_canonical"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_key: Mapped[str] = mapped_column(String(512), index=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    preferred_source_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    preferred_source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    detail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    title: Mapped[str] = mapped_column(String(1024))
    solicitation_number: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    agency: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subagency: Mapped[str | None] = mapped_column(String(512), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    buyer_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    state_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(256), nullable=True)
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    response_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_raw: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deadline_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_tz_confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="OPEN", index=True)
    procurement_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    set_aside: Mapped[str | None] = mapped_column(String(256), nullable=True)
    naics: Mapped[str | None] = mapped_column(String(16), nullable=True)
    psc: Mapped[str | None] = mapped_column(String(32), nullable=True)
    commodity_codes_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    estimated_value_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    contact_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    document_links_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    amendment_links_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    qa_links_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    submission_method: Mapped[str | None] = mapped_column(String(256), nullable=True)
    product_classification: Mapped[str] = mapped_column(String(64), default="UNKNOWN", index=True)
    research_priority: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    research_priority_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    interesting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    obvious_blocker: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_status: Mapped[str] = mapped_column(String(64), default="NEW", index=True)
    trust_tier: Mapped[int] = mapped_column(Integer, default=3)
    title_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    description_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("gt_contracts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OpportunitySighting(Base):
    """Each source sighting of a canonical opportunity (dedup retains all)."""

    __tablename__ = "gt_opportunity_sightings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_gt_sighting_source_ext"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discovered_opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("gt_discovered_opportunities.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(256), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    trust_tier: Mapped[int] = mapped_column(Integer, default=3)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoveryRun(Base):
    """Persistent discovery run metrics."""

    __tablename__ = "gt_discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sources_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sources_successful: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    raw_notices_seen: Mapped[int] = mapped_column(Integer, default=0)
    new_opportunities: Mapped[int] = mapped_column(Integer, default=0)
    updated_opportunities: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    core_product_count: Mapped[int] = mapped_column(Integer, default=0)
    product_plus_service_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    service_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    documents_discovered: Mapped[int] = mapped_column(Integer, default=0)
    external_request_counts_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
