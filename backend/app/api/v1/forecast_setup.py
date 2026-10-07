"""History, product cost, and the sales adjustment Horizonte reads."""
import re
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant_access import bind_tenant
from app.models.product import Product
from app.models.sale_month import SaleMonth
from app.models.site import Site
from app.services.forecast import build_horizon, save_adjust

router = APIRouter()


_THOUSANDS = re.compile(r"^\d{1,3}(\.\d{3})+$")


def _amount(raw, label: str) -> float:
    try:
        if isinstance(raw, str):
            text = raw.strip().replace(" ", "").replace("€", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            elif _THOUSANDS.fullmatch(text):
                text = text.replace(".", "")
            value = float(text)
        else:
            value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{label} no se puede leer")
    if value < 0 or value > 100_000_000:
        raise HTTPException(status_code=422, detail=f"{label} no se puede leer")
    return round(value, 2)


async def _site(db, tenant_id, site_id) -> Site:
    found = await db.execute(select(Site).where(Site.id == site_id, Site.tenant_id == tenant_id, Site.active == 1))
    site = found.scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=404, detail="Ese local no está en esta cuenta.")
    return site


@router.get("/horizonte")
async def horizonte(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await build_horizon(db, current_user)


@router.put("/projection")
async def projection(
    tenant_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    try:
        percent = float(payload.get("adjust_percent"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="El ajuste tiene que ser un porcentaje.")
    if percent < -90 or percent > 300:
        raise HTTPException(status_code=422, detail="El ajuste tiene que estar entre -90 y 300.")
    saved = await save_adjust(db, tenant_id, round(percent, 2))
    return {"adjust_percent": saved}


@router.get("/sale-months")
async def list_sale_months(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = (
        await db.execute(
            select(SaleMonth).where(SaleMonth.tenant_id == tenant_id).order_by(SaleMonth.year, SaleMonth.month)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "site_id": str(row.site_id),
                "year": row.year,
                "month": row.month,
                "amount": row.amount,
                "source": row.source,
            }
            for row in rows
        ]
    }


@router.post("/sale-months")
async def add_sale_months(
    tenant_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """One month, or a list. Their system can send the same body."""
    tenant_id = bind_tenant(current_user, tenant_id)
    items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
    saved = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Cada mes necesita año, mes e importe.")
        try:
            site_id = uuid.UUID(str(item.get("site_id") or payload.get("site_id")))
            year = int(item["year"])
            month = int(item["month"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Cada mes necesita local, año, mes e importe.")
        if year < 2000 or year > 2100 or month < 1 or month > 12:
            raise HTTPException(status_code=422, detail="Ese año o ese mes no sirven.")
        await _site(db, tenant_id, site_id)
        amount = _amount(item.get("amount"), "El importe")
        found = await db.execute(
            select(SaleMonth).where(
                SaleMonth.tenant_id == tenant_id,
                SaleMonth.site_id == site_id,
                SaleMonth.year == year,
                SaleMonth.month == month,
            )
        )
        row = found.scalar_one_or_none()
        source = "api" if item.get("source") == "api" else "manual"
        if row is None:
            row = SaleMonth(tenant_id=tenant_id, site_id=site_id, year=year, month=month, amount=amount, source=source)
            db.add(row)
        else:
            row.amount = amount
            row.source = source
        saved.append(row)
    await db.commit()
    return {"saved": len(saved)}


@router.get("/products")
async def list_products(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = (
        await db.execute(select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name))
    ).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "site_id": str(row.site_id) if row.site_id else None,
                "name": row.name,
                "sale_price": row.sale_price,
                "cost": row.cost,
            }
            for row in rows
        ]
    }


@router.post("/products")
async def add_product(
    tenant_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=422, detail="El producto necesita un nombre.")
    price = _amount(payload.get("sale_price"), "El precio")
    if price <= 0:
        raise HTTPException(status_code=422, detail="El precio tiene que ser mayor que cero.")
    cost = _amount(payload.get("cost", 0), "El costo")
    site_id = None
    if payload.get("site_id"):
        site_id = uuid.UUID(str(payload["site_id"]))
        await _site(db, tenant_id, site_id)
    row = Product(tenant_id=tenant_id, site_id=site_id, name=name, sale_price=price, cost=cost)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "name": row.name, "sale_price": row.sale_price, "cost": row.cost}


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    row = await db.scalar(select(Product).where(Product.id == product_id, Product.tenant_id == tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Ese producto no está en esta cuenta.")
    await db.delete(row)
    await db.commit()
    return {"deleted": product_id}
