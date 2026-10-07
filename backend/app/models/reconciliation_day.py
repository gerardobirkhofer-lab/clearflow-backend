"""One saved check per company per day. A later day does not rewrite an earlier one."""
from sqlalchemy import Column, Date, DateTime, Float, Integer, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class ReconciliationDay(Base):
    __tablename__ = "reconciliation_days"
    __table_args__ = (
        UniqueConstraint("tenant_id", "day", name="uq_reconciliation_day"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    matched_count = Column(Integer, nullable=False, default=0)
    matched_amount = Column(Float, nullable=False, default=0)
    open_bank_count = Column(Integer, nullable=False, default=0)
    open_provider_count = Column(Integer, nullable=False, default=0)
    place_open = Column(Text, nullable=True)
    checked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
