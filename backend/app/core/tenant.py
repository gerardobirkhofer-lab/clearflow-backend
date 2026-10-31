"""
Tenant context extraction.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Query

from .auth import get_current_user, CurrentUser


async def get_current_tenant(current_user: CurrentUser = Depends(get_current_user)) -> UUID:
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="User is not associated with a tenant")
    return current_user.tenant_id


def assert_tenant_access(requested: UUID | None, current_user: CurrentUser) -> UUID:
    """
    Phase 0 isolation: every data call is bound to the JWT tenant.
    If the client sends a tenant_id, it must match; otherwise use the JWT tenant.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=403, detail="User is not associated with a tenant")
    if requested is not None and requested != current_user.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="No puedes acceder a datos de otro negocio (tenant mismatch)",
        )
    return current_user.tenant_id


async def resolve_tenant_id(
    tenant_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> UUID:
    """FastAPI dependency: query ?tenant_id= must match JWT, else JWT tenant wins."""
    return assert_tenant_access(tenant_id, current_user)
