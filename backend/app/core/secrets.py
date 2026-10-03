"""Production secret checks and encryption for stored provider credentials."""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_JWT_SECRET = "clearflow-secret-key-change-in-production"
_ENCRYPTED_PREFIX = "enc:v1:"


def jwt_secret() -> str:
    return os.getenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET)


def assert_production_secrets() -> None:
    """Refuse to boot a production process with a known or short signing key."""
    if os.getenv("ENVIRONMENT", "development") != "production":
        return
    secret = os.getenv("JWT_SECRET_KEY", "")
    if secret == DEFAULT_JWT_SECRET or len(secret) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY must be set to a unique secret of at least 32 characters "
            "when ENVIRONMENT=production"
        )
    if not os.getenv("STRIPE_WEBHOOK_SECRET", "").strip():
        raise RuntimeError("STRIPE_WEBHOOK_SECRET must be set when ENVIRONMENT=production")


def _fernet() -> Fernet:
    digest = hashlib.sha256(jwt_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value or value.startswith(_ENCRYPTED_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _ENCRYPTED_PREFIX + token


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    if not value.startswith(_ENCRYPTED_PREFIX):
        return value
    token = value[len(_ENCRYPTED_PREFIX):].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored credential could not be decrypted") from exc


def mask_secret(value: str | None) -> str:
    """Show only the last four characters of an account number."""
    if not value:
        return ""
    visible = value[-4:]
    return f"••••{visible}"
