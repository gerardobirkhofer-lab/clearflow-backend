from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class ProviderTransaction(Base):
    __tablename__ = "provider_transactions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider_name = Column(String(100), nullable=False)  # stripe, tpv, paypal, etc.
    filename = Column(String(255))
    concept = Column(Text)
    amount = Column(Float, nullable=False)
    transaction_date = Column(DateTime)
    reference = Column(String(255))
    raw_data = Column(Text)
    matched = Column(Integer, default=0)  # 0 = pending, 1 = matched
    matched_bank_tx_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
