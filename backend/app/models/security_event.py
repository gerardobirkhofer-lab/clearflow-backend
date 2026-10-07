"""A record of who saved or removed bank data. The detail never holds a full account number."""
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    action = Column(String(64), nullable=False)
    target_type = Column(String(64), nullable=True)
    target_id = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
