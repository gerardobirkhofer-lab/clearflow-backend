from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Enum
from sqlalchemy.sql import func
from app.core.database import Base
import enum

class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    WRITTEN_OFF = "written_off"

class DisputeType(str, enum.Enum):
    MISSING_PAYOUT = "missing_payout"
    WRONG_AMOUNT = "wrong_amount"
    DUPLICATE_CHARGE = "duplicate_charge"
    FEE_DISCREPANCY = "fee_discrepancy"
    LATE_SETTLEMENT = "late_settlement"

class Dispute(Base):
    __tablename__ = "disputes"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=True)
    provider_name = Column(String(50))
    dispute_type = Column(String(30), default="missing_payout")
    status = Column(String(20), default="open")
    amount = Column(Float)
    currency = Column(String(3), default="EUR")
    description = Column(Text)
    opened_at = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    expected_resolution = Column(DateTime, nullable=True)
    days_to_resolve = Column(Integer, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    recovery_amount = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
