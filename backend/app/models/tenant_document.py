from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, DateTime, Text, BigInteger, Index
from sqlalchemy.sql import func
from app.core.database import Base


class TenantDocument(Base):
    __tablename__ = "tenant_documents"
    __table_args__ = (
        Index("ix_tenant_docs_tenant_cat", "tenant_id", "category"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    category = Column(String(40), nullable=False, default="other", index=True)
    # contract | bank_evidence | provider_statement | other
    original_filename = Column(String(255), nullable=False)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(BigInteger, default=0)
    storage_backend = Column(String(32), nullable=False, default="clearflow_local")
    # clearflow_local | clearflow_minio | byo_s3 (stub)
    storage_key = Column(String(500), nullable=False)
    checksum_sha256 = Column(String(64), nullable=True)
    related_provider_name = Column(String(100), nullable=True, index=True)
    related_dispute_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True)
