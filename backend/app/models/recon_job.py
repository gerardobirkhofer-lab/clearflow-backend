from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.core.database import Base


class ReconJob(Base):
    """Persisted reconciliation job — survives process restart."""

    __tablename__ = "recon_jobs"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    progress = Column(Integer, nullable=False, default=0)
    params_json = Column(Text, nullable=True)  # date window / engine snapshot
    result_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
