from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    type = Column(String(50), default="store")
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
