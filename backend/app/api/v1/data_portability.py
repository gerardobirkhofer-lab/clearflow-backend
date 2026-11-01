"""
Tenant data export + purge (portability / control de datos).

Honest scope: exports movements + evidence for the JWT tenant.
Does not claim dedicated DB / BYO cloud.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_shared_db
from app.core.tenant import assert_tenant_access
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction
from app.models.recon_job import ReconJob

router = APIRouter()


class PurgeRequest(BaseModel):
    confirm: str = Field(..., description='Must be exactly "DELETE"')
    tenant_id: uuid.UUID | None = None


def _bank_dict(t: BankTransaction) -> dict:
    return {
        "id": t.id,
        "concept": t.concept,
        "amount": t.amount,
        "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
        "reference": t.reference,
        "bank_name": t.bank_name,
        "filename": t.filename,
        "matched": bool(t.matched),
        "match_status": getattr(t, "match_status", None),
        "match_evidence": getattr(t, "match_evidence", None),
        "matched_at": t.matched_at.isoformat() if getattr(t, "matched_at", None) else None,
        "tx_kind": getattr(t, "tx_kind", None),
        "settlement_id": getattr(t, "settlement_id", None),
        "gross_amount": getattr(t, "gross_amount", None),
        "fee_amount": getattr(t, "fee_amount", None),
        "net_amount": getattr(t, "net_amount", None),
    }


def _prov_dict(t: ProviderTransaction) -> dict:
    return {
        "id": t.id,
        "provider_name": t.provider_name,
        "concept": t.concept,
        "amount": t.amount,
        "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
        "reference": t.reference,
        "filename": t.filename,
        "matched": bool(t.matched),
        "match_status": getattr(t, "match_status", None),
        "match_evidence": getattr(t, "match_evidence", None),
        "matched_at": t.matched_at.isoformat() if getattr(t, "matched_at", None) else None,
        "matched_bank_tx_id": getattr(t, "matched_bank_tx_id", None),
        "tx_kind": getattr(t, "tx_kind", None),
        "settlement_id": getattr(t, "settlement_id", None),
        "gross_amount": getattr(t, "gross_amount", None),
        "fee_amount": getattr(t, "fee_amount", None),
        "net_amount": getattr(t, "net_amount", None),
    }


@router.get("/export")
async def export_tenant_data(
    tenant_id: uuid.UUID | None = Query(default=None),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    limit: int = Query(default=5000, ge=1, le=20000),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Download bank + provider movements (+ evidence) for the authenticated tenant.
    """
    tid = assert_tenant_access(tenant_id, current_user)

    banks = (
        await db.execute(
            select(BankTransaction)
            .where(BankTransaction.tenant_id == tid)
            .order_by(BankTransaction.transaction_date.desc().nullslast())
            .limit(limit)
        )
    ).scalars().all()
    provs = (
        await db.execute(
            select(ProviderTransaction)
            .where(ProviderTransaction.tenant_id == tid)
            .order_by(ProviderTransaction.transaction_date.desc().nullslast())
            .limit(limit)
        )
    ).scalars().all()

    bank_total = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(BankTransaction.tenant_id == tid)
    ) or 0
    prov_total = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(ProviderTransaction.tenant_id == tid)
    ) or 0

    meta = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "tenant_id": str(tid),
        "disclaimer": (
            "Export de partición lógica ClearFlow (tenant_id). "
            "No implica base de datos dedicada ni BYO cloud."
        ),
        "limits": {"per_table": limit},
        "counts": {
            "bank_exported": len(banks),
            "bank_total": bank_total,
            "provider_exported": len(provs),
            "provider_total": prov_total,
            "truncated": bank_total > len(banks) or prov_total > len(provs),
        },
    }

    if format == "json":
        payload = {
            **meta,
            "bank_transactions": [_bank_dict(b) for b in banks],
            "provider_transactions": [_prov_dict(p) for p in provs],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return StreamingResponse(
            iter([body]),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="clearflow_export_{tid}.json"'
            },
        )

    # CSV: two sections concatenated with a marker row
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["# meta", json.dumps(meta, ensure_ascii=False)])
    writer.writerow([])
    writer.writerow(["# bank_transactions"])
    bank_cols = [
        "id", "transaction_date", "concept", "amount", "reference", "bank_name",
        "filename", "matched", "match_status", "settlement_id", "tx_kind", "match_evidence",
    ]
    writer.writerow(bank_cols)
    for b in banks:
        d = _bank_dict(b)
        writer.writerow([d.get(c) for c in bank_cols])
    writer.writerow([])
    writer.writerow(["# provider_transactions"])
    prov_cols = [
        "id", "transaction_date", "provider_name", "concept", "amount", "reference",
        "filename", "matched", "match_status", "settlement_id", "tx_kind",
        "matched_bank_tx_id", "gross_amount", "fee_amount", "net_amount", "match_evidence",
    ]
    writer.writerow(prov_cols)
    for p in provs:
        d = _prov_dict(p)
        writer.writerow([d.get(c) for c in prov_cols])

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="clearflow_export_{tid}.csv"'
        },
    )


@router.get("/summary")
async def data_summary(
    tenant_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tid = assert_tenant_access(tenant_id, current_user)
    bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(BankTransaction.tenant_id == tid)
    ) or 0
    prov = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tid
        )
    ) or 0
    confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tid,
            BankTransaction.matched == 1,
        )
    ) or 0
    return {
        "tenant_id": str(tid),
        "bank_transactions": bank,
        "provider_transactions": prov,
        "confirmed_bank": confirmed,
        "pending_bank": max(0, bank - confirmed),
        "disclaimer": "Partición lógica por tenant_id en la nube ClearFlow.",
    }


@router.post("/purge")
async def purge_tenant_data(
    body: PurgeRequest,
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Delete movements + recon jobs for the tenant. Irreversible.
    Requires confirm="DELETE". Does not delete the Tenant row / login user.
    """
    if body.confirm != "DELETE":
        raise HTTPException(
            status_code=400,
            detail='Debés enviar confirm="DELETE" para borrar los datos del tenant',
        )
    tid = assert_tenant_access(body.tenant_id, current_user)

    bank_n = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(BankTransaction.tenant_id == tid)
    ) or 0
    prov_n = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tid
        )
    ) or 0

    await db.execute(delete(BankTransaction).where(BankTransaction.tenant_id == tid))
    await db.execute(delete(ProviderTransaction).where(ProviderTransaction.tenant_id == tid))
    try:
        await db.execute(delete(ReconJob).where(ReconJob.tenant_id == tid))
    except Exception:
        pass
    # Keep OB connections but clear last sync markers? Safer: leave connections.
    await db.commit()

    return {
        "status": "purged",
        "tenant_id": str(tid),
        "deleted": {
            "bank_transactions": bank_n,
            "provider_transactions": prov_n,
        },
        "note": (
            "Movimientos y jobs borrados. Usuario/tenant y conexiones OB se conservan. "
            "Esto no es borrado de una DB dedicada — es purge de la partición lógica."
        ),
    }
