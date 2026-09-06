"""
Authentication dependencies.
Decodes JWT and resolves the real user from the database.
"""
from __future__ import annotations

from typing import Optional
import os
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

from app.core.database import SharedSessionLocal

security = HTTPBearer(auto_error=False)

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"


class CurrentUser:
    def __init__(self, id: UUID, email: str, tenant_id: UUID, role: str = "VIEWER", full_name: str | None = None):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role
        self.full_name = full_name


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id_str = payload.get("user_id")
    email = payload.get("email")
    if not user_id_str or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    # Resolve real user from database
    async with SharedSessionLocal() as session:
        from app.models_orm import User, Tenant
        from sqlalchemy import select
        from uuid import uuid4

        # Try to parse user_id as UUID (real users)
        try:
            user_uuid = UUID(user_id_str)
            result = await session.execute(select(User).where(User.id == user_uuid))
            user = result.scalar_one_or_none()
        except ValueError:
            # Demo token fallback: user_id is not a UUID (e.g. "1")
            # Find or create a demo user for smooth transition
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if not user:
                # Ensure demo tenant exists
                demo_tenant_id = UUID("22222222-2222-2222-2222-222222222222")
                t_result = await session.execute(select(Tenant).where(Tenant.id == demo_tenant_id))
                tenant = t_result.scalar_one_or_none()
                if not tenant:
                    tenant = Tenant(
                        id=demo_tenant_id,
                        name="Demo Tenant",
                        slug="demo",
                        timezone="Europe/Madrid",
                        currency="EUR",
                        is_active=True,
                        subscription_plan="free",
                        tier="starter",
                    )
                    session.add(tenant)
                    await session.flush()
                user = User(
                    id=uuid4(),
                    tenant_id=demo_tenant_id,
                    email=email,
                    password_hash=None,
                    full_name="Demo User",
                    role="OWNER",
                    auth_provider="demo",
                    is_active=True,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)

        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

        return CurrentUser(
            id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            full_name=user.full_name,
        )
    async with SharedSessionLocal() as session:
        from app.models_orm import User
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.id == UUID(user_id_str)))
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

        return CurrentUser(
            id=user.id,
            email=user.email,
            tenant_id=user.tenant_id,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            full_name=user.full_name,
        )


def require_role(allowed_roles: list[str]):
    """Dependency factory that checks if the current user has an allowed role."""
    async def role_checker(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if current_user.role.upper() not in [r.upper() for r in allowed_roles]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user
    return role_checker
