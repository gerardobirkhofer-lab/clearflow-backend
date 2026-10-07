"""A month of past sales for one place, typed or sent by their system."""
from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class SaleMonth(Base):
    __tablename__ = "sale_months"
    __table_args__ = (
        UniqueConstraint("tenant_id", "site_id", "year", "month", name="uq_sale_month_place"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    site_id = Column(UUID, nullable=False, index=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    source = Column(String(20), nullable=False, default="manual")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
