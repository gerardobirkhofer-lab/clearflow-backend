import uuid
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, select, func

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant_access import bind_tenant
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction
from app.models.reconciliation_day import ReconciliationDay
from app.services.open_matching import match_open_items, record_day

router = APIRouter()

LIST_LIMIT = 50


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _money(value) -> float:
    return round(float(value or 0), 2)


async def account_reconciliation(db: AsyncSession, tenant_id) -> dict:
    """Counts and rows actually stored for this tenant. No sample figures."""
    bank_agg = await db.execute(
        select(
            func.count(BankTransaction.id),
            func.coalesce(func.sum(case((BankTransaction.matched == 1, 1), else_=0)), 0),
            func.coalesce(func.sum(case((BankTransaction.matched == 0, 1), else_=0)), 0),
            func.coalesce(func.sum(case((BankTransaction.matched == 1, BankTransaction.amount), else_=0)), 0),
        ).where(BankTransaction.tenant_id == tenant_id)
    )
    bank_count, bank_matched, bank_pending, matched_amount = bank_agg.one()

    prov_agg = await db.execute(
        select(
            func.count(ProviderTransaction.id),
            func.coalesce(func.sum(case((ProviderTransaction.matched == 1, 1), else_=0)), 0),
            func.coalesce(func.sum(case((ProviderTransaction.matched == 0, 1), else_=0)), 0),
        ).where(ProviderTransaction.tenant_id == tenant_id)
    )
    prov_count, prov_matched, prov_pending = prov_agg.one()

    unmatched_bank_rows = (
        await db.execute(
            select(BankTransaction)
            .where(BankTransaction.tenant_id == tenant_id, BankTransaction.matched == 0)
            .order_by(BankTransaction.transaction_date.desc())
            .limit(LIST_LIMIT)
        )
    ).scalars().all()
    unmatched_prov_rows = (
        await db.execute(
            select(ProviderTransaction)
            .where(ProviderTransaction.tenant_id == tenant_id, ProviderTransaction.matched == 0)
            .order_by(ProviderTransaction.transaction_date.desc())
            .limit(LIST_LIMIT)
        )
    ).scalars().all()
    pair_rows = (
        await db.execute(
            select(ProviderTransaction, BankTransaction)
            .join(BankTransaction, ProviderTransaction.matched_bank_tx_id == BankTransaction.id)
            .where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.matched == 1,
            )
            .order_by(BankTransaction.transaction_date.desc())
            .limit(LIST_LIMIT)
        )
    ).all()

    matched = [
        {
            "score": None,
            "bank": {
                "id": bank.id,
                "concept": bank.concept or "",
                "amount": _money(bank.amount),
                "date": _iso(bank.transaction_date),
            },
            "provider": {
                "id": prov.id,
                "provider_name": prov.provider_name or "",
                "concept": prov.concept or "",
                "amount": _money(prov.amount),
                "date": _iso(prov.transaction_date),
            },
        }
        for prov, bank in pair_rows
    ]
    summary = {
        "total_bank": int(bank_count or 0),
        "total_provider": int(prov_count or 0),
        "matched_count": int(bank_matched or 0),
        "unmatched_bank_count": int(bank_pending or 0),
        "unmatched_provider_count": int(prov_pending or 0),
        "matched_amount": _money(matched_amount),
    }
    return {
        "matched_count": summary["matched_count"],
        "unmatched_bank_count": summary["unmatched_bank_count"],
        "unmatched_provider_count": summary["unmatched_provider_count"],
        "total_bank": summary["total_bank"],
        "total_provider": summary["total_provider"],
        "matched_amount": summary["matched_amount"],
        "summary": summary,
        "matched": matched,
        "unmatched_bank": [
            {
                "id": row.id,
                "concept": row.concept or "",
                "amount": _money(row.amount),
                "date": _iso(row.transaction_date),
            }
            for row in unmatched_bank_rows
        ],
        "unmatched_provider": [
            {
                "id": row.id,
                "provider_name": row.provider_name or "",
                "concept": row.concept or "",
                "amount": _money(row.amount),
                "date": _iso(row.transaction_date),
            }
            for row in unmatched_prov_rows
        ],
        "bank_transactions": summary["total_bank"],
        "provider_transactions": summary["total_provider"],
        "matched_bank": summary["matched_count"],
        "matched_provider": int(prov_matched or 0),
        "pending_bank": summary["unmatched_bank_count"],
        "pending_provider": summary["unmatched_provider_count"],
    }


@router.post("/run")
async def run_reconciliation(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    await match_open_items(db, tenant_id)
    await record_day(db, tenant_id)
    return await account_reconciliation(db, tenant_id)


@router.get("/status")
async def get_reconciliation_status(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    return await account_reconciliation(db, tenant_id)


@router.get("/days")
async def list_reconciliation_days(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Each saved day. Yesterday stays as it was when that day was checked."""
    tenant_id = bind_tenant(current_user, tenant_id)
    rows = await db.execute(
        select(ReconciliationDay)
        .where(ReconciliationDay.tenant_id == tenant_id)
        .order_by(ReconciliationDay.day.desc())
        .limit(14)
    )
    return {
        "items": [
            {
                "day": row.day.isoformat(),
                "matched_count": row.matched_count,
                "matched_amount": row.matched_amount,
                "open_bank_count": row.open_bank_count,
                "open_provider_count": row.open_provider_count,
                "checked_at": _iso(row.checked_at),
            }
            for row in rows.scalars().all()
        ]
    }
