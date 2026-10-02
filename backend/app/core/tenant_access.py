"""Keep request-supplied tenant ids inside the signed-in account."""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status

from .auth import CurrentUser


def bind_tenant(current_user: CurrentUser, requested: uuid.UUID | None = None) -> uuid.UUID:
    """Return the caller's tenant. A different requested id is a cross-account access."""
    if requested is not None and requested != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant access denied",
        )
    return current_user.tenant_id
