"""
API Router: Tenants
Handles tenant CRUD, tier upgrades, and provisioning.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db, get_shared_db
from ...core.auth import get_current_user, CurrentUser
from ...core.tenant import get_current_tenant
from ...core.tenant_access import bind_tenant
from .companies import companies_for_user, create_company, require_owner
from ...models_orm import Tenant
from ...schemas import (
    TenantCreate,
    TenantResponse,
    TenantUpgradeRequest,
    TenantListResponse,
)

router = APIRouter()


@router.get("/me", response_model=TenantResponse)
async def get_my_tenant(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get the current user's tenant."""
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.post("/", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    data: TenantCreate,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Add another company for this owner. The account's first company stays in place."""
    return await create_company(db, current_user, data.name, data.timezone, data.currency)


@router.get("/", response_model=TenantListResponse)
async def list_tenants(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Return every company this person is allowed to open."""
    return await companies_for_user(db, current_user)


@router.put("/upgrade", response_model=TenantResponse)
async def upgrade_tenant(
    data: TenantUpgradeRequest,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Change the plan on the caller's own tenant. Dedicated database URLs are not client input."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant.tier = data.tier
    tenant.subscription_plan = data.tier.value
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.put("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update a company this person owns."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if "name" in data:
        tenant.name = data["name"]
    if "timezone" in data:
        tenant.timezone = data["timezone"]
    if "currency" in data:
        tenant.currency = data["currency"]

    await db.commit()
    await db.refresh(tenant)
    return tenant
