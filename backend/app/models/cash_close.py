"""Immutable cash-close snapshot — motor proposal signed by a user for a period."""
from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, DateTime, Text, Date
from sqlalchemy.sql import func
from app.core.database import Base


class CashClose(Base):
    __tablename__ = "cash_closes"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    date_from = Column(Date, nullable=False)
    date_to = Column(Date, nullable=False)
    status = Column(String(32), nullable=False, default="signed")  # signed | void
    signed_by_user_id = Column(String(64), nullable=True)
    signed_by_email = Column(String(255), nullable=True)
    signed_at = Column(DateTime, nullable=False, server_default=func.now())
    # Snapshot counts at sign time
    motor_confirmed = Column(Integer, nullable=False, default=0)
    user_confirmed = Column(Integer, nullable=False, default=0)
    suggested_remaining = Column(Integer, nullable=False, default=0)
    pending_remaining = Column(Integer, nullable=False, default=0)
    bank_total = Column(Integer, nullable=False, default=0)
    allow_open_items = Column(Integer, nullable=False, default=0)  # 1 if override
    snapshot_json = Column(Text, nullable=True)
    evidence_hash = Column(String(64), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
