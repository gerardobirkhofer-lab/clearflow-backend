import uuid
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from datetime import datetime, timedelta
from collections import defaultdict

from app.core.database import get_db
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

router = APIRouter()


@router.post("/run")
async def run_reconciliation(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
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

    # ── Step 4: Build unmatched provider list ──
    unmatched_provider = []
    for p in prov_rows:
        pid, p_amount, p_date, p_ref, p_conc = p
        if pid not in used_prov_ids:
            unmatched_provider.append({
                "id": pid,
                "provider_name": '',
                "concept": p_conc or '',
                "amount": p_amount,
                "date": p_date.isoformat() if p_date else None,
            })

    return {
        "matched_count": matched_count,
        "unmatched_bank_count": len(unmatched_bank),
        "unmatched_provider_count": len(unmatched_provider),
        "total_bank": total_bank,
        "total_provider": len(prov_rows),
    }


@router.get("/status")
async def get_reconciliation_status(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    bank_result = await db.execute(
        select(BankTransaction).where(BankTransaction.tenant_id == tenant_id)
    )
    prov_result = await db.execute(
        select(ProviderTransaction).where(ProviderTransaction.tenant_id == tenant_id)
    )

    bank_txs = bank_result.scalars().all()
    prov_txs = prov_result.scalars().all()

    return {
        "bank_transactions": len(bank_txs),
        "provider_transactions": len(prov_txs),
        "matched_bank": sum(1 for b in bank_txs if b.matched),
        "matched_provider": sum(1 for p in prov_txs if p.matched),
        "pending_bank": sum(1 for b in bank_txs if not b.matched),
        "pending_provider": sum(1 for p in prov_txs if not p.matched),
    }
