"""
Authentication with JWT validation and a LocalAuthUser lookup.
Each authenticated user is bound to their own tenant. Missing or invalid
credentials are rejected.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select

security = HTTPBearer(auto_error=False)

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"

# Historical shared workspace. Never use it as an authenticated identity.
SHARED_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class CurrentUser:
    def __init__(self, id, email: str, tenant_id: uuid.UUID, role: str = "OWNER"):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    """Decode JWT, load LocalAuthUser, and return that user's own tenant."""
    if not credentials or not credentials.credentials or credentials.credentials in ("null", "undefined", ""):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = credentials.credentials
    try:
        import jwt
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub") or payload.get("user_id")
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    from app.core.database import SharedSessionLocal
    from app.models.local_auth_user import LocalAuthUser

    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == user_id_int)
        )
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

        tenant_id = user.tenant_id
        if tenant_id is not None and not isinstance(tenant_id, uuid.UUID):
            tenant_id = uuid.UUID(str(tenant_id))
        if tenant_id is None or tenant_id == SHARED_TENANT_ID:
            from app.models_orm import Tenant, TenantTier

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
            tenant_id = user.tenant_id
            if not isinstance(tenant_id, uuid.UUID):
                tenant_id = uuid.UUID(str(tenant_id))

        return CurrentUser(
            id=user.id,
            email=user.email,
            tenant_id=tenant_id,
            role=user.role.upper() if user.role else "OWNER",
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
