"""
Authentication endpoints — local auth with bcrypt against LocalAuthUser.
Registration gives each account its own tenant.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt
import jwt

from app.core.auth import SHARED_TENANT_ID
from app.core.database import SharedSessionLocal
from app.models.local_auth_user import LocalAuthUser

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
    """Register a new user into the local auth table."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or data.get("name") or email.split("@")[0]

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")
    if len(password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    async with SharedSessionLocal() as session:
        from sqlalchemy import select
        from app.models_orm import Tenant, TenantTier

        existing = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        tenant_id = uuid.uuid4()
        session.add(Tenant(
            id=tenant_id,
            name=full_name,
            slug=f"user-{tenant_id.hex}",
            timezone="UTC",
            currency="USD",
            is_active=True,
            subscription_plan="free",
            tier=TenantTier.STARTER,
        ))
        user = LocalAuthUser(
            email=email,
            password_hash=_hash_password(password),
            name=full_name,
            role="self_owner",
            is_active=1,
            tenant_id=tenant_id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = _create_access_token(user.id, email)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "tenant_id": str(user.tenant_id),
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
        from app.models_orm import Tenant, TenantTier

        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not _verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        tenant_id = user.tenant_id
        if tenant_id is not None and not isinstance(tenant_id, uuid.UUID):
            tenant_id = uuid.UUID(str(tenant_id))
        if tenant_id is None or tenant_id == SHARED_TENANT_ID:
            tenant_id = uuid.uuid4()
            session.add(Tenant(
                id=tenant_id,
                name=user.name or user.email,
                slug=f"user-{tenant_id.hex}",
                timezone="UTC",
                currency="USD",
                is_active=True,
                subscription_plan="free",
                tier=TenantTier.STARTER,
            ))
            user.tenant_id = tenant_id
            await session.commit()
            await session.refresh(user)

    token = _create_access_token(user.id, user.email)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        },
    }
