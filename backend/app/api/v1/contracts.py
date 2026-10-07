"""A confirmed contract: the fee and the day the money should arrive."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.secrets import encrypt_secret
from app.core.tenant_access import bind_tenant
from app.models.provider import Provider
from app.services.contract_terms import WEEKDAYS, propose_terms, text_from_file

router = APIRouter()
MAX_BYTES = 5_000_000


def _owner(current_user: CurrentUser, tenant_id: uuid.UUID) -> None:
    if current_user.company_roles.get(tenant_id) != "owner":
        raise HTTPException(status_code=403, detail="Only the company owner can do this")


def _public(row: Provider) -> dict:
    return {
        "id": row.id,
        "provider_name": row.name,
        "fee_percent": row.fee_percent or 0,
        "fee_fixed": row.fee_fixed or 0,
        "payout_days": row.credit_delay_days,
        "close_weekday": row.batch_day_of_week,
        "filename": row.contract_filename,
    }


async def _read_upload(file: UploadFile) -> tuple[str, str]:
    content = await file.read()
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="The contract is too large")
    filename = file.filename or "contrato.txt"
    try:
        text = text_from_file(filename, content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Use a PDF or a text file")
    except Exception:
        raise HTTPException(status_code=400, detail="The file could not be read")
    return filename, text


@router.post("/{tenant_id}/contracts/read")
async def read_contract(
    tenant_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    filename, text = await _read_upload(file)
    proposal = propose_terms(text)
    proposal["filename"] = filename
    return proposal


@router.get("/{tenant_id}/contracts")
async def list_contracts(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = (
        await db.execute(
            select(Provider)
            .where(Provider.tenant_id == tenant_id, Provider.terms_confirmed == 1)
            .order_by(Provider.name)
        )
    ).scalars().all()
    return {"items": [_public(row) for row in rows]}


@router.post("/{tenant_id}/contracts")
async def save_contract(
    tenant_id: uuid.UUID,
    provider_name: str = Form(...),
    fee_percent: float = Form(0),
    fee_fixed: float = Form(0),
    payout_days: str = Form(""),
    close_weekday: str = Form(""),
    file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    _owner(current_user, tenant_id)
    name = provider_name.strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=422, detail="Name the company in the contract")
    if fee_percent < 0 or fee_percent > 100 or fee_fixed < 0 or fee_fixed >= 10000:
        raise HTTPException(status_code=422, detail="The fee is not a usable number")
    days = None
    if str(payout_days).strip() != "":
        try:
            days = int(str(payout_days).strip())
        except ValueError:
            raise HTTPException(status_code=422, detail="The number of days is not usable")
        if days < 0 or days > 60:
            raise HTTPException(status_code=422, detail="The number of days is not usable")
    weekday = close_weekday.strip().lower()
    if weekday and weekday not in WEEKDAYS:
        raise HTTPException(status_code=422, detail="The close day is not a weekday")

    filename = None
    body = None
    if file is not None and file.filename:
        filename, text = await _read_upload(file)
        body = encrypt_secret(text)

    found = await db.execute(
        select(Provider).where(
            Provider.tenant_id == tenant_id,
            func.lower(Provider.name) == name.lower(),
        )
    )
    row = found.scalar_one_or_none()
    if row is None:
        row = Provider(
            tenant_id=tenant_id,
            name=name,
            provider_type="contract",
        )
        db.add(row)
    row.name = name
    row.fee_percent = fee_percent
    row.fee_fixed = fee_fixed
    row.credit_delay_days = days
    row.batch_day_of_week = weekday or None
    row.terms_confirmed = 1
    if filename:
        row.contract_filename = filename
        row.contract_body = body
    await db.commit()
    await db.refresh(row)
    return _public(row)
