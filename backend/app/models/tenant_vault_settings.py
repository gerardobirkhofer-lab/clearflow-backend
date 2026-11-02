from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.core.database import Base


class TenantVaultSettings(Base):
    """Per-tenant document storage preference. Always stored in shared meta-DB."""
    __tablename__ = "tenant_vault_settings"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, unique=True, index=True)
    backend = Column(String(32), nullable=False, default="clearflow_local")
    # clearflow_local | s3
    s3_endpoint = Column(String(255), nullable=True)  # e.g. s3.amazonaws.com or MinIO URL
    s3_bucket = Column(String(255), nullable=True)
    s3_region = Column(String(64), nullable=True, default="eu-west-1")
    s3_access_key_enc = Column(Text, nullable=True)
    s3_secret_key_enc = Column(Text, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, server_default=func.now())
