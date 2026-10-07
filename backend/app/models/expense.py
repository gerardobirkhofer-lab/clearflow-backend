"""Approximate monthly costs the client typed: rent, wages, and the rest."""
from sqlalchemy import Column, Date, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    concept = Column(String(80), nullable=False)
    amount = Column(Float, nullable=False)
    due_day = Column(Integer, nullable=True)
    due_on = Column(Date, nullable=True)
    site_id = Column(UUID, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
