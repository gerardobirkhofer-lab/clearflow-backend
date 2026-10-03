"""Companies an owner can open, the sites inside each one, and who may manage them."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import _hash_password
from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_shared_db
from app.core.tenant_access import bind_tenant
from app.models.company_membership import CompanyMembership
from app.models.local_auth_user import LocalAuthUser
from app.models.site import Site
from app.models_orm import Tenant, TenantTier

router = APIRouter()

SITE_KINDS = {"restaurant", "bar", "chiringuito", "apartments"}
MIN_PASSWORD_LENGTH = 10


def require_owner(current_user: CurrentUser, tenant_id: uuid.UUID) -> None:
    if current_user.company_roles.get(tenant_id) != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the company owner can do this")


def _as_uuid(value) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _site_out(site: Site) -> dict:
    return {"id": _as_uuid(site.id), "name": site.name, "kind": site.kind}


def _company_out(tenant: Tenant, role: str, sites: list[Site]) -> dict:
    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "timezone": tenant.timezone,
        "currency": tenant.currency,
        "is_active": tenant.is_active,
        "subscription_plan": tenant.subscription_plan,
        "subscription_expires_at": tenant.subscription_expires_at,
        "tier": tenant.tier,
        "created_at": tenant.created_at,
        "updated_at": tenant.updated_at,
        "role": role,
        "sites": [_site_out(site) for site in sites],
    }


async def companies_for_user(db: AsyncSession, current_user: CurrentUser) -> dict:
    roles = {_as_uuid(key): value for key, value in current_user.company_roles.items()}
    if not roles:
        return {"items": []}
    tenant_ids = list(roles)
    tenant_rows = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
    tenants = list(tenant_rows.scalars().all())
    site_rows = await db.execute(select(Site).where(Site.tenant_id.in_(tenant_ids)))
    grouped: dict[uuid.UUID, list[Site]] = {}
    for site in site_rows.scalars().all():
        grouped.setdefault(_as_uuid(site.tenant_id), []).append(site)
    items = [
        _company_out(tenant, roles.get(_as_uuid(tenant.id), "manager"), grouped.get(_as_uuid(tenant.id), []))
        for tenant in tenants
    ]
    items.sort(key=lambda item: (item["name"] or "").lower())
    return {"items": items}


async def create_company(db: AsyncSession, current_user: CurrentUser, name: str, timezone: str, currency: str) -> dict:
    if "owner" not in current_user.company_roles.values():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an owner can add a company")
    label = (name or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="Company name is required")
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        id=tenant_id,
        name=label[:255],
        slug=f"co-{tenant_id.hex[:16]}",
        timezone=timezone or "Europe/Madrid",
        currency=(currency or "EUR")[:3],
        is_active=True,
        subscription_plan="free",
        tier=TenantTier.STARTER,
    )
    db.add(tenant)
    db.add(CompanyMembership(user_id=current_user.id, tenant_id=tenant_id, role="owner"))
    await db.commit()
    await db.refresh(tenant)
    return _company_out(tenant, "owner", [])


@router.get("")
async def list_companies(
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await companies_for_user(db, current_user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_company(
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await create_company(
        db,
        current_user,
        data.get("name") or "",
        data.get("timezone") or "Europe/Madrid",
        data.get("currency") or "EUR",
    )


@router.get("/{tenant_id}/sites")
async def list_sites(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = await db.execute(select(Site).where(Site.tenant_id == tenant_id).order_by(Site.name))
    return {"items": [_site_out(site) for site in rows.scalars().all()]}


@router.post("/{tenant_id}/sites", status_code=status.HTTP_201_CREATED)
async def add_site(
    tenant_id: uuid.UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    name = (data.get("name") or "").strip()
    kind = (data.get("kind") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="Site name is required")
    if kind not in SITE_KINDS:
        raise HTTPException(status_code=422, detail="Site kind must be restaurant, bar, chiringuito, or apartments")
    site = Site(id=uuid.uuid4(), tenant_id=tenant_id, name=name[:255], kind=kind)
    db.add(site)
    await db.commit()
    await db.refresh(site)
    return _site_out(site)


@router.post("/{tenant_id}/members", status_code=status.HTTP_201_CREATED)
async def add_manager(
    tenant_id: uuid.UUID,
    data: dict,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Add a manager who can open this company and no other."""
    tenant_id = bind_tenant(current_user, tenant_id)
    require_owner(current_user, tenant_id)
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")

    found = await db.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
    user = found.scalar_one_or_none()
    if user is None:
        password = data.get("password") or ""
        full_name = (data.get("name") or data.get("full_name") or "").strip()
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=422, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if not full_name:
            raise HTTPException(status_code=422, detail="Name is required for a new manager")
        user = LocalAuthUser(
            email=email,
            password_hash=_hash_password(password),
            name=full_name[:100],
            role="manager",
            is_active=1,
            tenant_id=tenant_id,
        )
        db.add(user)
        await db.flush()

    existing = await db.execute(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.tenant_id == tenant_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(CompanyMembership(user_id=user.id, tenant_id=tenant_id, role="manager"))
    await db.commit()
    return {"email": user.email, "name": user.name, "role": "manager", "tenant_id": str(tenant_id)}
