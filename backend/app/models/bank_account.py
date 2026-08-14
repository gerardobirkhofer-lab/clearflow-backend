from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String(100), nullable=False)
    bank_name = Column(String(100), nullable=True)
    account_number = Column(String(100), nullable=True)
    iban = Column(String(100), nullable=True)
    currency = Column(String(10), default="EUR")
    opening_balance = Column(Float, nullable=True)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
