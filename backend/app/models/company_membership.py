"""Which companies a person can open, and whether they own or only manage them."""
from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy import DateTime

from app.core.database import Base
from app.core.uuid_type import UUID


class CompanyMembership(Base):
    __tablename__ = "company_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_company_membership_user_tenant"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    role = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
