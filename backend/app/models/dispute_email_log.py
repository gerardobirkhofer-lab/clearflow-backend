from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base


class DisputeEmailLog(Base):
    __tablename__ = "dispute_email_logs"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    provider_name = Column(String(50), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(3), default="EUR")
    concept = Column(String(255))
    description = Column(Text)
    date = Column(String(20))  # Transaction date as string (YYYY-MM-DD)
    days_open = Column(Integer, default=0)
    recipient_email = Column(String(255), nullable=False)
    status = Column(String(20), default="sent")  # sent, failed
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, server_default=func.now())
