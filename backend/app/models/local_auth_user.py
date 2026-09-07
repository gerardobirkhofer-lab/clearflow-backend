"""
Local auth user model — separate table to avoid conflicts with existing users table.
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class LocalAuthUser(Base):
    """Local authentication users — completely separate from legacy users table."""
    __tablename__ = "cf_local_users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    role = Column(String(50), default="self_owner")
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
