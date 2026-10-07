"""Card charges the client sends from their own Redsys portal."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant_access import bind_tenant
from app.models.card_operation import CardOperation
from app.models.site import Site

router = APIRouter()


def _amount(raw, label: str) -> float:
    try:
        if isinstance(raw, str):
            text = raw.strip().replace(" ", "").replace("€", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            value = float(text)
        else:
            value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{label} no se puede leer")
    if value < 0 or value > 100_000_000:
        raise HTTPException(status_code=422, detail=f"{label} no se puede leer")
    return round(value, 2)


@router.get("/card-operations")
async def list_card_operations(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = (
        await db.execute(
            select(CardOperation).where(CardOperation.tenant_id == tenant_id).order_by(CardOperation.operated_at)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "site_id": str(row.site_id),
                "operated_at": row.operated_at.isoformat() if row.operated_at else None,
                "amount": row.amount,
                "auth_code": row.auth_code,
                "terminal": row.terminal,
                "reference": row.reference,
                "fee_amount": row.fee_amount,
            }
            for row in rows
        ]
    }


@router.post("/card-operations")
async def add_card_operations(
    tenant_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """One operation, or a list, from the commerce portal the client opened for us."""
    tenant_id = bind_tenant(current_user, tenant_id)
    items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
    saved = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Cada operación necesita local, fecha e importe.")
        try:
            site_id = uuid.UUID(str(item.get("site_id") or payload.get("site_id")))
            when = datetime.fromisoformat(str(item.get("operated_at") or item.get("sold_on")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Cada operación necesita local, fecha e importe.")
        found = await db.execute(select(Site).where(Site.id == site_id, Site.tenant_id == tenant_id, Site.active == 1))
        if found.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Ese local no está en esta cuenta.")
        amount = _amount(item.get("amount"), "El importe")
        if amount <= 0:
            raise HTTPException(status_code=422, detail="El importe de la operación no se puede leer")
        fee = item.get("fee_amount")
        fee_amount = None if fee in (None, "") else _amount(fee, "La comisión")
        row = CardOperation(
            tenant_id=tenant_id,
            site_id=site_id,
            operated_at=when,
            amount=amount,
            auth_code=(str(item.get("auth_code")).strip()[:40] if item.get("auth_code") else None),
            terminal=(str(item.get("terminal")).strip()[:40] if item.get("terminal") else None),
            reference=(str(item.get("reference")).strip()[:80] if item.get("reference") else None),
            fee_amount=fee_amount,
        )
        db.add(row)
        saved.append(row)
    await db.commit()
    return {"saved": len(saved)}
