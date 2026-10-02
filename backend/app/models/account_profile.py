"""Company setup saved for an account. One row per tenant."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, Text
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class AccountProfile(Base):
    __tablename__ = "cf_account_profiles"

    tenant_id = Column(UUID, primary_key=True)
    payload = Column(Text, nullable=False, default="{}")
    onboarding_complete = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
