from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class ExpectedCollection(Base):
    __tablename__ = "expected_collections"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    sale_date = Column(DateTime, nullable=False)
    gross_amount = Column(Float, nullable=False)
    fee_amount = Column(Float, default=0.0)
    net_amount = Column(Float, nullable=False)
    expected_deposit_date = Column(DateTime, nullable=False)
    card_type = Column(String(20), nullable=True)
    reference = Column(String(255), nullable=True)
    status = Column(String(50), default="pending")
    matched_bank_tx_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
