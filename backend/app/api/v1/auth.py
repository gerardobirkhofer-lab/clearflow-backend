"""
Authentication endpoints. Each account gets its own tenant, stored on the user
and copied into the JWT. Request handlers still load the tenant from the database.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
import bcrypt
import jwt
from sqlalchemy import select

from app.core.database import SharedSessionLocal
from app.models.local_auth_user import LocalAuthUser

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _create_access_token(user_id: int, email: str, tenant_id: uuid.UUID) -> str:
    """Encode a JWT with user claims, including the account tenant."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _as_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _create_tenant_for_account(session, name: str, email: str) -> uuid.UUID:
    """Persist a tenant that belongs only to this account."""
    from app.models_orm import Tenant, TenantTier

    tenant_id = uuid.uuid4()
    label = (name or email.split("@")[0] or "account").strip()[:255]
    tenant = Tenant(
        id=tenant_id,
        name=label or "account",
        slug=f"acct-{tenant_id.hex[:16]}",
        timezone="Europe/Madrid",
        currency="EUR",
        is_active=True,
        subscription_plan="free",
        tier=TenantTier.STARTER,
    )
    session.add(tenant)
    await session.flush()
    return tenant_id


def _user_out(user: LocalAuthUser) -> dict:
    tenant_id = _as_uuid(user.tenant_id)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "tenant_id": str(tenant_id) if tenant_id else None,
    }


@router.post("/register", status_code=201)
async def register(data: dict, request: Request):
    """Register a user and a tenant that belongs only to that account."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or data.get("name") or email.split("@")[0]

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")
    if len(password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    async with SharedSessionLocal() as session:
        existing = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        tenant_id = await _create_tenant_for_account(session, full_name, email)
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

    token = _create_access_token(user.id, email, _as_uuid(user.tenant_id))
    return {"token": token, "user": _user_out(user)}


@router.post("/login")
async def login(data: dict):
    """Authenticate and ensure the account has its own tenant stored and signed."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password required")

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        user = result.scalar_one_or_none()
        if user is None or not _verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        tenant_id = _as_uuid(user.tenant_id)
        if tenant_id is None:
            tenant_id = await _create_tenant_for_account(session, user.name, user.email)
            user.tenant_id = tenant_id
            await session.commit()
            await session.refresh(user)
            tenant_id = _as_uuid(user.tenant_id)

    token = _create_access_token(user.id, user.email, tenant_id)
    return {"token": token, "user": _user_out(user)}
