"""Login and reset attempts stored in the database so a restart does not clear them."""
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.core.database import Base


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"

    id = Column(Integer, primary_key=True, index=True)
    attempt_key = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
