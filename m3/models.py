"""Authoritative business records for M3 2.0 Build 001."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from m3.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class RoleName(str, enum.Enum):
    owner = "owner"
    supplier_manager = "supplier_manager"
    contract_specialist = "contract_specialist"
    custom = "custom"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(64), default=RoleName.contract_specialist.value)
    # Extra permission keys beyond role defaults
    extra_permissions: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Only the original bootstrap owner — not delegable via role assignment
    is_primary_owner: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Bumped on deactivate / password reset to invalidate cookies
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    # active = shown on Team; archived = removed from team but attribution preserved
    team_status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    pending_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(128))  # user email or "system:research"
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    origin: Mapped[str] = mapped_column(String(32), default="manual")  # manual | automated
    research_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class SourceRegistry(Base):
    __tablename__ = "source_registry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(1024))
    source_kind: Mapped[str] = mapped_column(String(64))  # socrata | state_portal | opengov | manufacturer | other
    information_available: Mapped[str] = mapped_column(Text, default="")
    retrieval_method: Mapped[str] = mapped_column(String(64), default="http_get")
    search_capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)
    document_access: Mapped[str] = mapped_column(String(64), default="unknown")
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False)
    access_cost: Mapped[str] = mapped_column(String(32), default="free")
    documented_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    experimentally_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_history: Mapped[list] = mapped_column(JSONB, default=list)
    known_limitations: Mapped[str] = mapped_column(Text, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    adapter: Mapped[str] = mapped_column(String(64), default="http_listing")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    objective: Mapped[str] = mapped_column(String(64), index=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    sources_tried: Mapped[list] = mapped_column(JSONB, default=list)
    sources_succeeded: Mapped[list] = mapped_column(JSONB, default=list)
    limitations: Mapped[list] = mapped_column(JSONB, default=list)
    result_snapshot: Mapped[list] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Opportunity(Base):
    """Saved opportunities only — discovery hits are not auto-persisted."""

    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    external_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    solicitation_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geo_state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    geo_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    posted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    procurement_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_category: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    set_aside: Mapped[str | None] = mapped_column(String(64), nullable=True)
    set_aside_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_value_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_value_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_value_unknown: Mapped[bool] = mapped_column(Boolean, default=True)
    value_estimate_method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value_evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    official_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    documents_available: Mapped[str] = mapped_column(String(32), default="unknown")
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requirement_class: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    quantity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # saved | pursuing | archived | not_interested
    status: Mapped[str] = mapped_column(String(32), default="saved", index=True)
    # operator | acceptance_harness | ambiguous
    data_origin: Mapped[str] = mapped_column(String(64), default="operator", index=True)
    display_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    saved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    raw_evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SearchProfile(Base):
    __tablename__ = "search_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="contracts")  # contracts | suppliers
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    website: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Company-level contact (separate from individual sales contacts)
    business_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_type: Mapped[str | None] = mapped_column(String(64), nullable=True)  # headquarters | sales_office | …
    general_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    general_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    supplier_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # operator | acceptance_harness | ambiguous
    data_origin: Mapped[str] = mapped_column(String(64), default="operator", index=True)
    product_categories: Mapped[list] = mapped_column(JSONB, default=list)
    brands: Mapped[list] = mapped_column(JSONB, default=list)
    geo_coverage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relationship_status: Mapped[str] = mapped_column(String(64), default="lead")
    reseller_application_status: Mapped[str] = mapped_column(String(64), default="not_started")
    credit_application_status: Mapped[str] = mapped_column(String(64), default="not_started")
    account_approval_status: Mapped[str] = mapped_column(String(64), default="not_started")
    dealer_authorization_status: Mapped[str] = mapped_column(String(64), default="not_started")
    reseller_application_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    credit_application_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_credit_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(128), nullable=True)
    personal_guarantee_required: Mapped[str] = mapped_column(String(32), default="unknown")
    personal_credit_required: Mapped[str] = mapped_column(String(32), default="unknown")
    minimum_order: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shipping_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    dropship_capability: Mapped[str] = mapped_column(String(32), default="unknown")
    discounts_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_available: Mapped[str] = mapped_column(String(32), default="unknown")
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified")
    source_urls: Mapped[list] = mapped_column(JSONB, default=list)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    follow_up_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contacts: Mapped[list[SupplierContact]] = relationship(back_populates="supplier", cascade="all, delete-orphan")
    notes: Mapped[list[SupplierNote]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class SupplierContact(Base):
    __tablename__ = "supplier_contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    supplier: Mapped[Supplier] = relationship(back_populates="contacts")


class SupplierNote(Base):
    __tablename__ = "supplier_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"), index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="note")  # note | conversation | follow_up
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped[Supplier] = relationship(back_populates="notes")


class CatalogUpload(Base):
    __tablename__ = "catalog_uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"), index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    uncertain_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CatalogProduct(Base):
    __tablename__ = "catalog_products"
    __table_args__ = (UniqueConstraint("catalog_upload_id", "row_index", name="uq_catalog_row"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    catalog_upload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("catalog_uploads.id"), index=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"), index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mpn: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    supplier_sku: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    specifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_of_measure: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_qty: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    availability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extraction_uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    source_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class UserInvitation(Base):
    """Primary-owner invitation tokens — single-use, expiring, invitation-only signup."""

    __tablename__ = "user_invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), index=True)
    invited_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(64), default=RoleName.contract_specialist.value)
    extra_permissions: Mapped[dict] = mapped_column(JSONB, default=dict)
    # new_user | email_change
    purpose: Mapped[str] = mapped_column(String(32), default="new_user")
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)  # pending | accepted | revoked | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Fix forward refs for relationship typing under from __future__ annotations
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    pass
