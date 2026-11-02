from app.core.uuid_type import UUID
from sqlalchemy import Column, Integer, String, DateTime, Text, LargeBinary, ForeignKey
from sqlalchemy.sql import func
from app.core.database import Base


class WebAuthnCredential(Base):
    """Passkey / WebAuthn credential (Face ID / Touch ID capable on supporting devices)."""
    __tablename__ = "webauthn_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("cf_local_users.id"), nullable=False, index=True)
    tenant_id = Column(UUID, nullable=False, index=True)
    credential_id = Column(String(512), nullable=False, unique=True, index=True)
    public_key = Column(LargeBinary, nullable=False)
    sign_count = Column(Integer, default=0)
    device_type = Column(String(32), nullable=True)  # platform | cross-platform
    backed_up = Column(Integer, default=0)
    transports = Column(String(120), nullable=True)  # json list as string
    nickname = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)
