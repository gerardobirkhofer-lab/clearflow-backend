"""Fernet helper for encrypting BYO credentials at rest."""
from __future__ import annotations

import base64
import hashlib
import os


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError("cryptography required") from e
    raw = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY") or "change-me-in-production"
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt_str(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str | None) -> str | None:
    if not token:
        return None
    return _fernet().decrypt(token.encode()).decode()
