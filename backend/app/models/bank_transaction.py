from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, nullable=True)
    filename = Column(String(255))
    bank_name = Column(String(100), nullable=True)
    transaction_date = Column(DateTime)
    concept = Column(Text)
    amount = Column(Float)
    balance = Column(Float, nullable=True)
    reference = Column(String(255), nullable=True)
    raw_data = Column(Text)
    matched = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
