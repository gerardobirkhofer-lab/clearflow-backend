from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Index
from sqlalchemy.sql import func
from app.core.database import Base

class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (
        Index('ix_bank_tx_tenant_date', 'tenant_id', 'transaction_date'),
        Index('ix_bank_tx_tenant_matched', 'tenant_id', 'matched'),
        Index('ix_bank_tx_tenant_amount', 'tenant_id', 'amount'),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    filename = Column(String(255))
    bank_name = Column(String(100), nullable=True)
    transaction_date = Column(DateTime, index=True)
    concept = Column(Text)
    amount = Column(Float, index=True)
    balance = Column(Float, nullable=True)
    reference = Column(String(255), nullable=True, index=True)
    raw_data = Column(Text)
    matched = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, server_default=func.now())
