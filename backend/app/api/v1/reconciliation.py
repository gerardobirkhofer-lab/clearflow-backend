import uuid
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, select, func, and_, text
from datetime import datetime, timedelta
from collections import defaultdict

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant_access import bind_tenant
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

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
    """
    Scalable reconciliation engine.
    Loads only lightweight tuples, indexes by amount, processes in batches,
    and updates matches with bulk SQL.
    """
    # ── Step 1: Load provider transactions as lightweight tuples ──
    # (id, amount, transaction_date, reference, concept)
    prov_result = await db.execute(
        select(
            ProviderTransaction.id,
            ProviderTransaction.amount,
            ProviderTransaction.transaction_date,
            ProviderTransaction.reference,
            ProviderTransaction.concept,
        ).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_rows = prov_result.all()

    # Index provider transactions by rounded amount bucket
    prov_by_amount = defaultdict(list)
    for p in prov_rows:
        pid, p_amount, p_date, p_ref, p_conc = p
        bucket = round(float(p_amount), 0) if p_amount is not None else 0
        prov_by_amount[bucket].append(p)
        prov_by_amount[bucket + 1].append(p)
        prov_by_amount[bucket - 1].append(p)

    # ── Step 2: Stream bank transactions in batches ──
    batch_size = 2000
    matched_count = 0
    unmatched_bank = []
    used_prov_ids = set()

    bank_stmt = await db.execute(
        select(func.count()).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    )
    total_bank = bank_stmt.scalar_one_or_none() or 0

    offset = 0
    bank_matched_ids = []
    prov_matched_ids = []
    prov_matched_bank_ids = []

    while offset < total_bank:
        bank_batch_result = await db.execute(
            select(
                BankTransaction.id,
                BankTransaction.amount,
                BankTransaction.transaction_date,
                BankTransaction.reference,
                BankTransaction.concept,
            ).where(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.matched == 0,
            ).offset(offset).limit(batch_size)
        )
        bank_batch = bank_batch_result.all()

        if not bank_batch:
            break

        for b in bank_batch:
            b_id, b_amount, b_date, b_ref, b_conc = b
            b_amount_bucket = round(float(b_amount), 0) if b_amount is not None else 0

            # Gather candidates from same and adjacent amount buckets
            candidates = []
            for p in prov_by_amount.get(b_amount_bucket, []):
                if p[0] not in used_prov_ids:
                    candidates.append(p)
            for p in prov_by_amount.get(b_amount_bucket + 1, []):
                if p[0] not in used_prov_ids:
                    candidates.append(p)
            for p in prov_by_amount.get(b_amount_bucket - 1, []):
                if p[0] not in used_prov_ids:
                    candidates.append(p)

            best_match = None
            best_score = 0

            for p in candidates:
                pid, p_amount, p_date, p_ref, p_conc = p
                score = 0

                # Amount match
                if b_amount is not None and p_amount is not None:
                    amt_diff = abs(b_amount - p_amount)
                    if amt_diff < 0.01:
                        score += 3
                    elif amt_diff < 1:
                        score += 2
                    elif amt_diff < 5:
                        score += 1

                # Date match
                if b_date and p_date:
                    diff = abs((b_date - p_date).days)
                    if diff == 0:
                        score += 2
                    elif diff <= 3:
                        score += 1

                # Reference match
                if b_ref and p_ref and b_ref == p_ref:
                    score += 2

                # Concept fuzzy match
                if b_conc and p_conc:
                    b_conc_l = b_conc.lower()
                    p_conc_l = p_conc.lower()
                    if b_conc_l in p_conc_l or p_conc_l in b_conc_l:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_match = p

            if best_match and best_score >= 4:
                pid, p_amount, p_date, p_ref, p_conc = best_match
                used_prov_ids.add(pid)
                bank_matched_ids.append(b_id)
                prov_matched_ids.append(pid)
                prov_matched_bank_ids.append(b_id)
                matched_count += 1
            else:
                unmatched_bank.append({
                    "id": b_id,
                    "concept": b_conc or '',
                    "amount": b_amount,
                    "date": b_date.isoformat() if b_date else None,
                })

        offset += batch_size

    # ── Step 3: Bulk update matches ──
    if bank_matched_ids:
        # Update bank transactions
        await db.execute(
            text("""
                UPDATE bank_transactions 
                SET matched = 1 
                WHERE id = ANY(:ids)
            """),
            {"ids": bank_matched_ids}
        )

        # Update provider transactions
        for i in range(0, len(prov_matched_ids), 1000):
            batch_pids = prov_matched_ids[i:i+1000]
            batch_bids = prov_matched_bank_ids[i:i+1000]
            for pid, bid in zip(batch_pids, batch_bids):
                await db.execute(
                    text("""
                        UPDATE provider_transactions 
                        SET matched = 1, matched_bank_tx_id = :bid 
                        WHERE id = :pid
                    """),
                    {"pid": pid, "bid": bid}
                )

    await db.commit()

    return await account_reconciliation(db, tenant_id)


@router.get("/status")
async def get_reconciliation_status(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    return await account_reconciliation(db, tenant_id)
