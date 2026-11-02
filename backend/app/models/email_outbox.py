from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Index
from sqlalchemy.sql import func
from app.core.database import Base


class EmailOutbox(Base):
    """
    Client-gated outbound email. System creates drafts; client must approve
    before ClearFlow will send. Reject/cancel = work done, no interference.
    """
    __tablename__ = "email_outbox"
    __table_args__ = (
        Index("ix_email_outbox_tenant_status", "tenant_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    kind = Column(String(40), nullable=False, default="provider_dispute")
    # provider_dispute | bank_dispute
    status = Column(String(32), nullable=False, default="pending_approval", index=True)
    # draft | pending_approval | approved | rejected | sent | failed | cancelled
    dispute_id = Column(Integer, nullable=True, index=True)
    provider_name = Column(String(100), nullable=True)
    to_email = Column(String(255), nullable=True)
    subject = Column(String(500), nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)
    amount = Column(Float, default=0.0)
    currency = Column(String(3), default="EUR")
    concept = Column(String(255), nullable=True)
    meta_json = Column(Text, nullable=True)
    created_by = Column(String(20), default="system")  # system | user
    needs_recipient = Column(Integer, default=0)  # 1 if to_email missing
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
