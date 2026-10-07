"""Keep request-supplied tenant ids inside the signed-in account."""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status

from .auth import CurrentUser


def bind_tenant(current_user: CurrentUser, requested: uuid.UUID | None = None) -> uuid.UUID:
    """Return a company this person is allowed to open. Anything else is denied."""
    target = requested if requested is not None else current_user.tenant_id
    allowed = getattr(current_user, "company_roles", None) or {current_user.tenant_id: "owner"}
    if target not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant access denied",
        )
    return target
