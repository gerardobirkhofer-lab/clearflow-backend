"""
Authentication endpoints — real local auth with bcrypt.
Backward-compatible: demo token still works as fallback.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from sqlalchemy import select
import jwt

from app.core.database import SharedSessionLocal
from app.models_orm import User, Tenant, LocalCredential, UserRole

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def _create_access_token(user_id: uuid.UUID, email: str) -> str:
    """Encode a JWT with user claims."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "user_id": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/register", status_code=201)
async def register(data: dict, request: Request):
    """Register a new owner user + tenant + local credentials."""
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or data.get("name") or email.split("@")[0]

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")
    if len(password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    async with SharedSessionLocal() as session:
        # Check existing user
        existing = await session.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        # Create tenant
        tenant_slug = f"t-{uuid.uuid4().hex[:8]}"
        tenant = Tenant(
            id=uuid.uuid4(),
            name=data.get("company_name") or full_name,
            slug=tenant_slug,
            timezone="Europe/Madrid",
            currency="EUR",
            is_active=True,
            subscription_plan="free",
            tier="starter",
        )
        session.add(tenant)
        await session.flush()  # get tenant.id

        # Create user
        user = User(
            id=uuid.uuid4(),
            email=email,
            full_name=full_name,
            role=UserRole.OWNER,
            auth_provider="local",
            tenant_id=tenant.id,
            is_active=True,
        )
        session.add(user)
        await session.flush()  # get user.id

        # Create local credential
        cred = LocalCredential(
            id=uuid.uuid4(),
            user_id=user.id,
            password_hash=pwd_context.hash(password),
        )
        session.add(cred)

        await session.commit()

    token = _create_access_token(user.id, email)
    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.full_name,
            "role": user.role.value,
            "tenant_id": str(tenant.id),
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
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        result = await session.execute(
            select(LocalCredential).where(LocalCredential.user_id == user.id)
        )
        cred = result.scalar_one_or_none()
        if cred is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not pwd_context.verify(password, cred.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Update last login
        user.last_login_at = datetime.now(timezone.utc)
        await session.commit()

    token = _create_access_token(user.id, user.email)
    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.full_name,
            "role": user.role.value if hasattr(user.role, "value") else str(user.role),
            "tenant_id": str(user.tenant_id),
        },
    }
