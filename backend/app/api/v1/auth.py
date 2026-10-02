"""
Authentication endpoints. Each account gets its own tenant, stored on the user
and copied into the JWT. Request handlers still load the tenant from the database.
"""
from __future__ import annotations

import os
import time
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
MIN_PASSWORD_LENGTH = 10
_ATTEMPTS: dict[str, list[float]] = {}


def _client_key(request: Request, email: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    host = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"{host}:{email}"


def _rate_limit(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    recent = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < window_seconds]
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    recent.append(now)
    _ATTEMPTS[key] = recent


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
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    _rate_limit(f"register:{_client_key(request, email)}", limit=5, window_seconds=3600)

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
async def login(data: dict, request: Request):
    """Authenticate and ensure the account has its own tenant stored and signed."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password required")
    _rate_limit(f"login:{_client_key(request, email)}", limit=8, window_seconds=300)

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


def _reset_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "purpose": "password_reset",
        "iat": now,
        "exp": now + timedelta(minutes=30),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/forgot-password")
async def forgot_password(data: dict, request: Request):
    """Always return the same response so callers cannot learn which emails exist."""
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email required")
    _rate_limit(f"forgot:{_client_key(request, email)}", limit=5, window_seconds=3600)

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        user = result.scalar_one_or_none()

    if user is not None:
        token = _reset_token(user.id, user.email)
        from app.core.email import get_email_service

        frontend = os.getenv("FRONTEND_URL", "http://localhost:3000")
        link = f"{frontend}/login?reset={token}"
        await get_email_service().send_email(
            to=user.email,
            subject="Reset your ClearFlow password",
            body_text=f"Reset your password: {link}\nThis link expires in 30 minutes.",
        )
    return {"detail": "If that email is registered, a reset link is on its way."}


@router.post("/reset-password")
async def reset_password(data: dict, request: Request):
    token = data.get("token") or ""
    password = data.get("password") or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    _rate_limit(f"reset:{_client_key(request, 'token')}", limit=10, window_seconds=3600)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
    if payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Reset link is invalid or expired")

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or user.email != payload.get("email"):
            raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
        user.password_hash = _hash_password(password)
        await session.commit()
    return {"detail": "Password updated"}
