"""
Authentication endpoints — real local auth with bcrypt using legacy User table.
Backward-compatible: demo token still works as fallback.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt
import jwt

from app.core.database import SharedSessionLocal
from app.models.user import User as LegacyUser

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

security = HTTPBearer()


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def _verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def _create_access_token(user_id: int, email: str) -> str:
    """Encode a JWT with user claims."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "iat": now,
        "exp": now + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/register", status_code=201)
async def register(data: dict, request: Request):
    """Register a new user into the legacy users table."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or data.get("name") or email.split("@")[0]

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")
    if len(password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    async with SharedSessionLocal() as session:
        # Check existing user
        from sqlalchemy import select
        existing = await session.execute(select(LegacyUser).where(LegacyUser.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        # Create user in legacy table
        user = LegacyUser(
            email=email,
            password_hash=_hash_password(password),
            name=full_name,
            role="self_owner",
            is_active=1,
        )
        session.add(user)
        await session.commit()
        # Refresh to get the auto-generated id
        await session.refresh(user)

    token = _create_access_token(user.id, email)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }


@router.post("/login")
async def login(data: dict):
    """Authenticate with email + password, return JWT."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password required")

    async with SharedSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(LegacyUser).where(LegacyUser.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not _verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_access_token(user.id, user.email)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }
