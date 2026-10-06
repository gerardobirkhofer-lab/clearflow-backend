"""A place that belongs to one company: a restaurant, bar, beach bar, or apartment book."""
import uuid

from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func

from app.core.database import Base
from app.core.uuid_type import UUID


class Site(Base):
    __tablename__ = "sites"

    id = Column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    location = Column(String(80), nullable=True)
    kind = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
