"""
Tenant context extraction.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException

from .auth import get_current_user, CurrentUser


async def get_current_tenant(current_user: CurrentUser = Depends(get_current_user)) -> UUID:
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="User is not associated with a tenant")
    return current_user.tenant_id
