"""One card charge from the merchant's Redsys portal. The client authorizes that portal."""
from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class CardOperation(Base):
    __tablename__ = "card_operations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    site_id = Column(UUID, nullable=False, index=True)
    operated_at = Column(DateTime, nullable=False)
    amount = Column(Float, nullable=False)
    auth_code = Column(String(40), nullable=True)
    terminal = Column(String(40), nullable=True)
    reference = Column(String(80), nullable=True)
    fee_amount = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
