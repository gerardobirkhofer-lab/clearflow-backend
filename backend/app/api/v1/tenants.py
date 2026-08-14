from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.tenant import Tenant

router = APIRouter()

@router.get("/")
async def list_tenants(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant).where(Tenant.owner_user_id == user_id))
    tenants = result.scalars().all()
    return {"tenants": [{"id": t.id, "name": t.name, "type": t.type} for t in tenants]}

@router.post("/")
async def create_tenant(data: dict, db: AsyncSession = Depends(get_db)):
    tenant = Tenant(
        name=data.get("name"),
        type=data.get("type", "store"),
        owner_user_id=data.get("owner_user_id"),
        is_active=1,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return {"id": tenant.id, "name": tenant.name, "type": tenant.type}

@router.put("/{tenant_id}")
async def update_tenant(tenant_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    if "name" in data:
        tenant.name = data["name"]
    if "type" in data:
        tenant.type = data["type"]
    
    await db.commit()
    await db.refresh(tenant)
    return {"id": tenant.id, "name": tenant.name, "type": tenant.type}

@router.delete("/{tenant_id}")
async def delete_tenant(tenant_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    await db.delete(tenant)
    await db.commit()
    return {"message": "Tenant deleted"}
