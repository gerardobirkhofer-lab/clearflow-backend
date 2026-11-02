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
    from ...core.database import tenant_db_manager

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
        await tenant_db_manager.close_tenant(tenant_id)
        tenant.database_url = None
        tenant.database_name = None

    tenant.tier = data.tier
    await db.commit()
    await db.refresh(tenant)
    # Invalidate URL cache so next request re-reads database_url and create_all
    await tenant_db_manager.close_tenant(tenant_id)
    if tenant.database_url:
        await tenant_db_manager.get_session_maker(tenant_id)
    return tenant


@router.post("/provision-db")
async def provision_dedicated_db(
    database_url: str,
    database_name: str | None = None,
    tier: str = "pro",
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """
    Attach a dedicated Postgres URL, create business tables, smoke-test SELECT 1,
    then set tier to pro/enterprise. Does not auto-create cloud DBs — you supply the URL.
    """
    from sqlalchemy import text
    from ...core.database import tenant_db_manager

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    tier_val = (tier or "pro").lower()
    if tier_val not in ("pro", "enterprise"):
        raise HTTPException(400, "tier must be pro or enterprise")
    if not database_url or not database_url.strip():
        raise HTTPException(400, "database_url is required")

    tenant.database_url = database_url.strip()
    tenant.database_name = database_name
    tenant.tier = TenantTier.PRO if tier_val == "pro" else TenantTier.ENTERPRISE
    await db.commit()
    await db.refresh(tenant)

    await tenant_db_manager.close_tenant(tenant_id)
    try:
        maker = await tenant_db_manager.get_session_maker(tenant_id)
        async with maker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        # Roll back URL so tenant stays usable on shared
        tenant.database_url = None
        tenant.database_name = None
        tenant.tier = TenantTier.STARTER
        await db.commit()
        await tenant_db_manager.close_tenant(tenant_id)
        raise HTTPException(
            400,
            detail=f"Dedicated DB unreachable or migrate failed: {exc}",
        ) from exc

    return {
        "ok": True,
        "tenant_id": str(tenant_id),
        "tier": tenant.tier.value if hasattr(tenant.tier, "value") else str(tenant.tier),
        "database_name": tenant.database_name,
        "message": "Dedicated DB provisioned (create_all + SELECT 1). Business traffic will route here.",
    }


@router.post("/migrate")
async def migrate_dedicated_db(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Re-run create_all on the tenant's dedicated DB (idempotent)."""
    from ...core.database import tenant_db_manager

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if not tenant.database_url:
        raise HTTPException(400, "No dedicated database_url — call provision-db first")

    await tenant_db_manager.close_tenant(tenant_id)
    try:
        await tenant_db_manager.get_session_maker(tenant_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail=f"migrate failed: {exc}") from exc

    return {
        "ok": True,
        "tenant_id": str(tenant_id),
        "message": "create_all completed on dedicated DB",
    }


@router.post("/activate")
async def activate_dedicated_db(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """
    Confirm dedicated routing is live: cache warm + SELECT 1.
    Call after provision-db / migrate.
    """
    from sqlalchemy import text
    from ...core.database import tenant_db_manager

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if not tenant.database_url:
        raise HTTPException(400, "No dedicated database_url configured")

    await tenant_db_manager.close_tenant(tenant_id)
    maker = await tenant_db_manager.get_session_maker(tenant_id)
    async with maker() as session:
        await session.execute(text("SELECT 1"))

    return {
        "ok": True,
        "tenant_id": str(tenant_id),
        "tier": tenant.tier.value if hasattr(tenant.tier, "value") else str(tenant.tier),
        "routing": "dedicated",
        "message": "Dedicated DB active for this tenant",
    }


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
