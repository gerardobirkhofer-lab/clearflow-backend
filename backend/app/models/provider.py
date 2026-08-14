from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String(100), nullable=False)
    provider_type = Column(String(50), nullable=False)
    settlement_mode = Column(String(50), default="per_transaction")
    credit_delay_days = Column(Integer, default=2)
    debit_delay_days = Column(Integer, default=1)
    transfer_delay_days = Column(Integer, default=0)
    batch_day_of_week = Column(String(20), nullable=True)
    batch_frequency = Column(String(20), nullable=True)
    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=True)
    fee_percent = Column(Float, default=0.0)
    fee_fixed = Column(Float, default=0.0)
    monthly_fee = Column(Float, default=0.0)
    contract_file_url = Column(String(500), nullable=True)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
