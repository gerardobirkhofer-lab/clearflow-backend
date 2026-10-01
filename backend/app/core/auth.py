"""
Authentication with JWT validation and a database lookup on cf_local_users.

The tenant always comes from the user row. A missing or invalid token is 401.
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


class CurrentUser:
    def __init__(self, id, email: str, tenant_id: uuid.UUID, role: str = "OWNER"):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role


def _unauthorized() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    """Decode JWT, load the user, and take tenant_id from the database."""
    if credentials is None:
        _unauthorized()

    token = credentials.credentials
    if not token or token in ("null", "undefined", ""):
        _unauthorized()

    try:
        import jwt
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        _unauthorized()

    user_id = payload.get("sub") or payload.get("user_id")
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        _unauthorized()

    from app.core.database import SharedSessionLocal
    from app.models.local_auth_user import LocalAuthUser

    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == user_id_int)
        )
        user = result.scalar_one_or_none()

    if user is None or user.tenant_id is None:
        _unauthorized()

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
