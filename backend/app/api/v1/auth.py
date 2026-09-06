"""
Authentication endpoints — real implementation with bcrypt and JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID
import os

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from passlib.context import CryptContext
import jwt

from app.core.database import get_db, SharedSessionLocal
from app.models_orm import User, Tenant, UserRole

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _create_access_token(user_id: UUID, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "user_id": str(user_id),
        "email": email,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/register", status_code=201)
async def register(data: dict, db: AsyncSession = Depends(get_db)):
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    name = data.get("name", "").strip()
    role_str = data.get("role", "self_owner").lower()

    if not email or not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Email and password (min 6 chars) required")

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=422, detail="Email already registered")

    # Create default tenant for new user
    tenant = Tenant(
        id=uuid4(),
        name=name or email.split("@")[0],
        slug=f"tenant-{uuid4().hex[:8]}",
        timezone="Europe/Madrid",
        currency="EUR",
        is_active=True,
        subscription_plan="free",
        tier="starter",
    )
    db.add(tenant)
    await db.flush()  # Get tenant.id without committing yet

    # Map frontend role strings to UserRole enum
    role_mapping = {
        "self_owner": UserRole.OWNER,
        "owner": UserRole.OWNER,
        "admin": UserRole.ADMIN,
        "accountant": UserRole.ACCOUNTANT,
    }
    role = role_mapping.get(role_str, UserRole.OWNER)

    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email=email,
        password_hash=pwd_context.hash(password),
        full_name=name or None,
        role=role,
        auth_provider="local",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = _create_access_token(user.id, user.email)

    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.full_name or user.email,
            "role": role_str,
            "tenant_id": str(tenant.id),
        },
    }


@router.post("/login")
async def login(data: dict, db: AsyncSession = Depends(get_db)):
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not pwd_context.verify(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    token = _create_access_token(user.id, user.email)

    role_str = user.role.value if hasattr(user.role, "value") else str(user.role)

    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.full_name or user.email,
            "role": role_str,
            "tenant_id": str(user.tenant_id),
        },
    }
