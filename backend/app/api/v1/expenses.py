"""Monthly costs the client keeps, and the cash outlook that uses them."""
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant_access import bind_tenant
from app.models.expense import Expense
from app.services.cash_outlook import KINDS, build_outlook

router = APIRouter()


def _amount(raw) -> float:
    if isinstance(raw, bool) or raw is None:
        raise HTTPException(status_code=422, detail="Escribe un importe aproximado.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().replace(" ", "").replace("€", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            raise HTTPException(status_code=422, detail="Escribe un importe aproximado.")
    if value <= 0:
        raise HTTPException(status_code=422, detail="El importe tiene que ser mayor que cero.")
    if value > 10_000_000:
        raise HTTPException(status_code=422, detail="El importe es demasiado grande.")
    return round(value, 2)


def _due(payload: dict) -> tuple[int | None, object]:
    from datetime import date

    due_day = payload.get("due_day")
    if due_day in (None, ""):
        day = None
    else:
        try:
            day = int(due_day)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="El día de pago tiene que ser un número del 1 al 31.")
        if day < 1 or day > 31:
            raise HTTPException(status_code=422, detail="El día de pago tiene que ser un número del 1 al 31.")
    raw = payload.get("due_on")
    if raw in (None, ""):
        return day, None
    try:
        return day, date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise HTTPException(status_code=422, detail="La fecha de esa factura no se puede leer.")


def _site(payload: dict):
    import uuid as uuid_lib

    raw = payload.get("site_id")
    if raw in (None, ""):
        return None
    try:
        return uuid_lib.UUID(str(raw))
    except ValueError:
        raise HTTPException(status_code=422, detail="Ese local no está en esta cuenta.")


def _clean(payload: dict) -> tuple[str, str, float, int | None, object, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Elige un tipo de gasto.")
    kind = str(payload.get("kind") or "").strip()
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail="Elige un tipo de gasto.")
    concept = str(payload.get("concept") or "").strip()
    if not concept:
        concept = KINDS[kind]
    if len(concept) > 80:
        raise HTTPException(status_code=422, detail="El concepto es demasiado largo.")
    due_day, due_on = _due(payload)
    return kind, concept, _amount(payload.get("amount")), due_day, due_on, _site(payload)


def _out(row: Expense) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KINDS.get(row.kind, row.kind),
        "concept": row.concept,
        "amount": round(float(row.amount or 0), 2),
        "due_day": row.due_day,
        "due_on": row.due_on.isoformat() if row.due_on else None,
        "site_id": str(row.site_id) if row.site_id else None,
    }


@router.get("")
async def list_expenses(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = (
        await db.execute(select(Expense).where(Expense.tenant_id == tenant_id).order_by(Expense.id.asc()))
    ).scalars().all()
    return {"items": [_out(row) for row in rows]}


@router.post("")
async def create_expense(
    tenant_id: uuid.UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    kind, concept, amount, due_day, due_on, site_id = _clean(payload)
    row = Expense(
        tenant_id=tenant_id,
        kind=kind,
        concept=concept,
        amount=amount,
        due_day=due_day,
        due_on=due_on,
        site_id=site_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{expense_id}")
async def delete_expense(
    expense_id: int,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    row = await db.scalar(
        select(Expense).where(Expense.id == expense_id, Expense.tenant_id == tenant_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Ese gasto no está en esta cuenta.")
    await db.delete(row)
    await db.commit()
    return {"deleted": expense_id}


@router.get("/outlook")
async def cash_outlook(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    return await build_outlook(db, tenant_id)
