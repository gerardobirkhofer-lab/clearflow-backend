"""
API Router: Tenants
Handles tenant CRUD, tier upgrades, and provisioning.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db, get_shared_db
from ...core.auth import get_current_user, require_role, CurrentUser
from ...core.tenant import get_current_tenant
from ...models_orm import Tenant, TenantTier
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
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
):
    """Create a new tenant (meta-DB write)."""
    # Check slug uniqueness
    result = await db.execute(select(Tenant).where(Tenant.slug == data.slug))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Slug already taken")

    tenant = Tenant(
        name=data.name,
        slug=data.slug,
        timezone=data.timezone,
        currency=data.currency,
        tier=data.tier,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("/", response_model=TenantListResponse)
async def list_tenants(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all tenants (admin only, meta-DB)."""
    result = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    tenants = result.scalars().all()
    return TenantListResponse(items=tenants)


@router.put("/upgrade", response_model=TenantResponse)
async def upgrade_tenant(
    data: TenantUpgradeRequest,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Upgrade or downgrade tenant tier and optionally set a dedicated DB URL."""
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Validate tier transition
    tier_order = {
        TenantTier.STARTER: 1,
        TenantTier.PRO: 2,
        TenantTier.ENTERPRISE: 3,
    }
    current_level = tier_order.get(tenant.tier, 1)
    new_level = tier_order.get(data.tier, 1)

    if new_level > current_level:
        # Upgrading
        if data.tier in (TenantTier.PRO, TenantTier.ENTERPRISE):
            if not data.database_url:
                raise HTTPException(
                    status_code=400,
                    detail=f"database_url is required to upgrade to {data.tier.value}",
                )
            tenant.database_url = data.database_url
            tenant.database_name = data.database_name
    elif new_level < current_level:
        # Downgrading
        from ...core.database import tenant_db_manager
        await tenant_db_manager.close_tenant(tenant_id)
        tenant.database_url = None
        tenant.database_name = None

    tenant.tier = data.tier
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.put("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
):
    """Update tenant name, timezone, etc."""
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
