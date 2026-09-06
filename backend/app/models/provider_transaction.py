from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Index
from sqlalchemy.sql import func
from app.core.database import Base

class ProviderTransaction(Base):
    __tablename__ = "provider_transactions"
    __table_args__ = (
        Index('ix_prov_tx_tenant_date', 'tenant_id', 'transaction_date'),
        Index('ix_prov_tx_tenant_matched', 'tenant_id', 'matched'),
        Index('ix_prov_tx_tenant_amount', 'tenant_id', 'amount'),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    provider_name = Column(String(100), nullable=False, index=True)  # stripe, tpv, paypal, etc.
    filename = Column(String(255))
    concept = Column(Text)
    amount = Column(Float, nullable=False, index=True)
    transaction_date = Column(DateTime, index=True)
    reference = Column(String(255), index=True)
    raw_data = Column(Text)
    matched = Column(Integer, default=0, index=True)  # 0 = pending, 1 = matched
    matched_bank_tx_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
