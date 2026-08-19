"""
Authentication stubs. Replace with real JWT/OAuth2 logic in Phase 5.
"""
from __future__ import annotations

from typing import Optional
import os
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, id: UUID, email: str, tenant_id: UUID, role: str = "ADMIN"):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    return CurrentUser(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        email="dev@clearflow.local",
        tenant_id=UUID(os.getenv("DEFAULT_TENANT_ID", "abdebf06-ae2e-4565-8111-162092005abc")),
        role="OWNER",
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
