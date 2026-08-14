"""
SQLAlchemy ORM models for ClearFlow.
All models include tenant_id for multi-tenant isolation.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    String, Integer, Date, DateTime, Numeric, Boolean, Text,
    ForeignKey, Index, UniqueConstraint, CheckConstraint,
    func, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


# ── Enums ──

class TransactionType(str, PyEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class ReconciliationStatus(str, PyEnum):
    PENDING = "pending"
    MATCHED = "matched"
    PARTIAL = "partial"
    UNMATCHED = "unmatched"
    DISCREPANCY = "discrepancy"
    CLEARED = "cleared"


class FeeType(str, PyEnum):
    PERCENTAGE = "percentage"
    FLAT = "flat"
    MIXED = "mixed"
    TIERED = "tiered"


class MovementType(str, PyEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    FEE = "fee"
    CHARGE = "charge"
    CHARGEBACK = "chargeback"


class UploadStatus(str, PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class UserRole(str, PyEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"


# ── Mixins ──

class TenantMixin:
    """Adds tenant_id to every table for row-level security."""
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class TimestampMixin:
    """Auto-managed created_at / updated_at."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Core Tables ──

class Tenant(Base):
    """A workspace / organization. All data is scoped to a tenant."""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Madrid")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    date_format: Mapped[str] = mapped_column(String(20), default="%d/%m/%Y")
    decimal_separator: Mapped[str] = mapped_column(String(1), default=",")
    thousands_separator: Mapped[str] = mapped_column(String(1), default=".")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    subscription_plan: Mapped[str] = mapped_column(String(20), default="free")
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    institutions: Mapped[list["Institution"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    collections: Mapped[list["CardCollection"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    bank_movements: Mapped[list["BankMovement"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    tpv_reports: Mapped[list["TPVClosingReport"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    fee_structures: Mapped[list["FeeStructure"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    reconciliation_results: Mapped[list["ReconciliationResult"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    cash_flow_entries: Mapped[list["CashFlowEntry"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    morning_reports: Mapped[list["MorningReport"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    file_uploads: Mapped[list["FileUpload"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    webhooks: Mapped[list["Webhook"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base, TenantMixin, TimestampMixin):
    """A user within a tenant workspace."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(ENUM(UserRole, name="user_role"), default=UserRole.VIEWER)
    auth_provider: Mapped[str] = mapped_column(String(50), default="auth0")  # auth0, clerk, local
    auth_subject: Mapped[str | None] = mapped_column(String(255), index=True)  # External auth ID
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_webhook: Mapped[bool] = mapped_column(Boolean, default=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class Institution(Base, TenantMixin, TimestampMixin):
    """A bank, acquirer, or clearing house."""
    __tablename__ = "institutions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_institution_tenant_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    institution_type: Mapped[str] = mapped_column(String(50), default="acquirer")  # bank, acquirer, clearing_house
    settlement_delay_days: Mapped[int] = mapped_column(Integer, default=1)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Banking API connection (optional)
    bank_connection_provider: Mapped[str | None] = mapped_column(String(50))  # nordigen, plaid, salt_edge
    bank_connection_requisition_id: Mapped[str | None] = mapped_column(String(255))
    bank_connection_account_iban: Mapped[str | None] = mapped_column(String(34))
    bank_connection_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_connection_status: Mapped[str | None] = mapped_column(String(20))  # connected, expired, error

    tenant: Mapped["Tenant"] = relationship(back_populates="institutions")
    fee_structures: Mapped[list["FeeStructure"]] = relationship(back_populates="institution")
    collections: Mapped[list["CardCollection"]] = relationship(back_populates="institution")
    bank_movements: Mapped[list["BankMovement"]] = relationship(back_populates="institution")
    tpv_reports: Mapped[list["TPVClosingReport"]] = relationship(back_populates="institution")


class FeeStructure(Base, TenantMixin, TimestampMixin):
    """Fee configuration per institution and card type."""
    __tablename__ = "fee_structures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    card_type: Mapped[TransactionType] = mapped_column(ENUM(TransactionType, name="transaction_type"), default=TransactionType.DEBIT)
    fee_type: Mapped[FeeType] = mapped_column(ENUM(FeeType, name="fee_type"), default=FeeType.PERCENTAGE)
    percentage_rate: Mapped[Decimal] = mapped_column(Numeric(8, 5), default=Decimal("0.00000"))
    flat_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    min_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tier_thresholds: Mapped[dict | None] = mapped_column(JSONB)  # { "1000": "0.010", "5000": "0.008" }
    vat_included: Mapped[bool] = mapped_column(Boolean, default=False)
    vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # 21.00 for 21%
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    tenant: Mapped["Tenant"] = relationship(back_populates="fee_structures")
    institution: Mapped["Institution"] = relationship(back_populates="fee_structures")


class CardCollection(Base, TenantMixin, TimestampMixin):
    """Daily card collection from TPV."""
    __tablename__ = "card_collections"
    __table_args__ = (
        Index("ix_collections_date_inst_status", "collection_date", "institution_id", "status"),
        Index("ix_collections_reference", "reference"),
        Index("ix_collections_tenant_date", "tenant_id", "collection_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    collection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    upload_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False
    )
    card_type: Mapped[TransactionType] = mapped_column(ENUM(TransactionType, name="transaction_type"), default=TransactionType.DEBIT)
    terminal_id: Mapped[str | None] = mapped_column(String(100))
    batch_number: Mapped[str | None] = mapped_column(String(100), index=True)
    reference: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    amount_gross: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    amount_net: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    card_last_digits: Mapped[str | None] = mapped_column(String(4))
    transaction_count: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReconciliationStatus] = mapped_column(
        ENUM(ReconciliationStatus, name="reconciliation_status"),
        default=ReconciliationStatus.PENDING,
        index=True,
    )
    matched_bank_movement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_movements.id", ondelete="SET NULL", use_alter=True)
    )
    matched_tpv_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tpv_closing_reports.id", ondelete="SET NULL", use_alter=True)
    )
    expected_settlement_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_data: Mapped[dict | None] = mapped_column(JSONB)  # Original upload row
    source_file_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_uploads.id", ondelete="SET NULL")
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="collections")
    institution: Mapped["Institution"] = relationship(back_populates="collections")
    reconciliation_result: Mapped["ReconciliationResult | None"] = relationship(
        back_populates="collection", uselist=False
    )


class BankMovement(Base, TenantMixin, TimestampMixin):
    """Line from a bank statement."""
    __tablename__ = "bank_movements"
    __table_args__ = (
        Index("ix_bank_mv_date_inst", "value_date", "institution_id"),
        Index("ix_bank_mv_reference", "reference"),
        Index("ix_bank_mv_matched", "matched_collection_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    value_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    booking_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False
    )
    account_iban: Mapped[str | None] = mapped_column(String(34))
    movement_type: Mapped[MovementType] = mapped_column(ENUM(MovementType, name="movement_type"), default=MovementType.CREDIT)
    reference: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    concept: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    matched_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card_collections.id", ondelete="SET NULL", use_alter=True)
    )
    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    source_file_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_uploads.id", ondelete="SET NULL")
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="bank_movements")
    institution: Mapped["Institution"] = relationship(back_populates="bank_movements")


class TPVClosingReport(Base, TenantMixin, TimestampMixin):
    """End-of-day report from POS terminal."""
    __tablename__ = "tpv_closing_reports"
    __table_args__ = (
        Index("ix_tpv_date_inst", "report_date", "institution_id"),
        Index("ix_tpv_batch", "batch_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    terminal_id: Mapped[str] = mapped_column(String(100), nullable=False)
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False
    )
    opening_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_sales_gross: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_refunds: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_net: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    debit_sales: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    credit_sales: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    batch_number: Mapped[str | None] = mapped_column(String(100), index=True)
    z_report_number: Mapped[str | None] = mapped_column(String(100))
    matched_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card_collections.id", ondelete="SET NULL", use_alter=True)
    )
    discrepancies: Mapped[str | None] = mapped_column(Text)
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    source_file_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_uploads.id", ondelete="SET NULL")
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="tpv_reports")
    institution: Mapped["Institution"] = relationship(back_populates="tpv_reports")


class ReconciliationResult(Base, TenantMixin, TimestampMixin):
    """Output of the matching engine."""
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        Index("ix_recon_collection", "collection_id"),
        Index("ix_recon_status_date", "status", "collection_date"),
        UniqueConstraint("tenant_id", "collection_id", name="uq_recon_tenant_collection"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card_collections.id", ondelete="CASCADE"), nullable=False
    )
    collection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    bank_movement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_movements.id", ondelete="SET NULL")
    )
    tpv_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tpv_closing_reports.id", ondelete="SET NULL")
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        ENUM(ReconciliationStatus, name="reconciliation_status"),
        default=ReconciliationStatus.PENDING,
        index=True,
    )
    match_confidence: Mapped[str | None] = mapped_column(String(20))  # high, medium, low
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    bank_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    tpv_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    calculated_fee: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    actual_fee_deduction: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    fee_discrepancy: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    amount_discrepancy: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    days_to_clear: Mapped[int | None] = mapped_column(Integer)
    uncleared_reason: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped["Tenant"] = relationship(back_populates="reconciliation_results")
    collection: Mapped["CardCollection"] = relationship(back_populates="reconciliation_result")


class CashFlowEntry(Base, TenantMixin, TimestampMixin):
    """A line in the cash flow forecast."""
    __tablename__ = "cash_flow_entries"
    __table_args__ = (
        Index("ix_cashflow_date_type", "entry_date", "entry_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(50), nullable=False)  # actual_balance, card_settlement, fee, other
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    source_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card_collections.id", ondelete="SET NULL")
    )
    expected_value_date: Mapped[date | None] = mapped_column(Date)
    running_balance: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    tenant: Mapped["Tenant"] = relationship(back_populates="cash_flow_entries")


class MorningReport(Base, TenantMixin, TimestampMixin):
    """Daily morning report."""
    __tablename__ = "morning_reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "report_date", name="uq_report_tenant_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Summary counts
    total_collections: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, default=0)
    discrepancy_count: Mapped[int] = mapped_column(Integer, default=0)

    # Cash position
    actual_bank_balance: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    projected_incoming_7d: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    projected_incoming_30d: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    total_pending_clearance: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # Fee summary
    total_fees_yesterday: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    fee_discrepancies: Mapped[list | None] = mapped_column(JSONB)

    # Alerts
    alerts: Mapped[list | None] = mapped_column(JSONB)

    # Detail references (stored as JSON arrays of IDs)
    uncleared_collection_ids: Mapped[list | None] = mapped_column(JSONB)
    discrepancy_ids: Mapped[list | None] = mapped_column(JSONB)

    # Report outputs
    report_html: Mapped[str | None] = mapped_column(Text)
    report_text: Mapped[str | None] = mapped_column(Text)
    report_pdf_url: Mapped[str | None] = mapped_column(String(500))

    # Delivery tracking
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_recipients: Mapped[list | None] = mapped_column(JSONB)
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped["Tenant"] = relationship(back_populates="morning_reports")


class FileUpload(Base, TenantMixin, TimestampMixin):
    """Tracks uploaded files and their processing status."""
    __tablename__ = "file_uploads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)  # collection, bank_statement, tpv_report
    status: Mapped[UploadStatus] = mapped_column(ENUM(UploadStatus, name="upload_status"), default=UploadStatus.PENDING)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSONB)  # [{row: 5, error: "Invalid amount"}]
    started_processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    tenant: Mapped["Tenant"] = relationship(back_populates="file_uploads")


class Webhook(Base, TenantMixin, TimestampMixin):
    """Outgoing webhook configuration per tenant."""
    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    events: Mapped[list] = mapped_column(JSONB, nullable=False)  # ["reconciliation.completed", "discrepancy.detected"]
    secret: Mapped[str] = mapped_column(String(255), nullable=False)  # For HMAC signature
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_status: Mapped[str | None] = mapped_column(String(20))  # success, failed
    failure_count: Mapped[int] = mapped_column(Integer, default=0)

    tenant: Mapped["Tenant"] = relationship(back_populates="webhooks")


class ApiKey(Base, TenantMixin, TimestampMixin):
    """API keys for programmatic access."""
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False, index=True)  # First 8 chars for display
    scopes: Mapped[list] = mapped_column(JSONB, default=list)  # ["read:collections", "write:bank-statements"]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")


class AuditLog(Base, TenantMixin):
    """Immutable audit trail for all data mutations."""
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    table_name: Mapped[str] = mapped_column(String(100), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # create, update, delete, resolve
    changed_fields: Mapped[dict | None] = mapped_column(JSONB)
    previous_values: Mapped[dict | None] = mapped_column(JSONB)
    new_values: Mapped[dict | None] = mapped_column(JSONB)
    performed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)

    tenant: Mapped["Tenant"] = relationship()


# ── Event Listeners for Audit Log ──

@event.listens_for(ReconciliationResult, "after_update")
def log_reconciliation_resolution(mapper, connection, target):
    """Log when a reconciliation result is manually resolved."""
    if target.resolved and target.resolved_at:
        # This would typically be handled by a background job or service layer
        # Kept simple here for demonstration
        pass
