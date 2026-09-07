"""
Authentication with real JWT validation + DB lookup using LocalAuthUser table.
Falls back to demo user for invalid/missing tokens (backward-compatible).
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

security = HTTPBearer(auto_error=False)

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"


class CurrentUser:
    def __init__(self, id, email: str, tenant_id: uuid.UUID, role: str = "OWNER"):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role


# Demo fallback user (preserves existing sessions)
DEMO_USER = CurrentUser(
    id=1,
    email="demo@clearflow.local",
    tenant_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    role="OWNER",
)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    """Decode JWT → lookup user in LocalAuthUser table → return CurrentUser.
    Falls back to DEMO_USER on any auth failure (backward compatibility)."""
    if not credentials:
        return DEMO_USER

    token = credentials.credentials
    if not token or token in ("null", "undefined", ""):
        return DEMO_USER

    # Try JWT decode
    try:
        import jwt
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub") or payload.get("user_id")
        if user_id is None:
            return DEMO_USER
    except Exception:
        return DEMO_USER

    # Lookup in LocalAuthUser table
    try:
        from app.core.database import SharedSessionLocal
        from app.models.local_auth_user import LocalAuthUser

        async with SharedSessionLocal() as session:
            result = await session.execute(
                select(LocalAuthUser).where(LocalAuthUser.id == int(user_id))
            )
            user = result.scalar_one_or_none()
            if user is None:
                return DEMO_USER
            return CurrentUser(
                id=user.id,
                email=user.email,
                tenant_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                role=user.role.upper() if user.role else "OWNER",
            )
    except Exception:
        return DEMO_USER


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
