import uuid
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import timedelta

from app.core.database import get_db
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

router = APIRouter()

@router.post("/run")
async def run_reconciliation(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Fetch all pending bank transactions
    bank_result = await db.execute(
        select(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0
        )
    )
    bank_txs = bank_result.scalars().all()

    # Fetch all pending provider transactions
    prov_result = await db.execute(
        select(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0
        )
    )
    prov_txs = prov_result.scalars().all()

    matched = []
    unmatched_bank = []
    unmatched_provider = []
    discrepancies = []

    # Simple matching: same amount + date within 3 days
    used_bank = set()
    used_prov = set()

    for b in bank_txs:
        best_match = None
        best_score = 0

        for p in prov_txs:
            if p.id in used_prov:
                continue

            score = 0
            # Amount match (exact = 3 points, within 1 cent = 2 points)
            if abs(b.amount - p.amount) < 0.01:
                score += 3
            elif abs(b.amount - p.amount) < 1:
                score += 2

            # Date match (same day = 2 points, within 3 days = 1 point)
            if b.transaction_date and p.transaction_date:
                diff = abs((b.transaction_date - p.transaction_date).days)
                if diff == 0:
                    score += 2
                elif diff <= 3:
                    score += 1

            # Reference match
            if b.reference and p.reference and b.reference == p.reference:
                score += 2

            if score > best_score:
                best_score = score
                best_match = p

        # Threshold: need at least 4 points (amount + date)
        if best_match and best_score >= 4:
            used_bank.add(b.id)
            used_prov.add(best_match.id)

            # Mark as matched
            b.matched = 1
            best_match.matched = 1
            best_match.matched_bank_tx_id = b.id

            matched.append({
                "bank": {"id": b.id, "concept": b.concept, "amount": b.amount, "date": b.transaction_date.isoformat() if b.transaction_date else None},
                "provider": {"id": best_match.id, "provider_name": best_match.provider_name, "concept": best_match.concept, "amount": best_match.amount, "date": best_match.transaction_date.isoformat() if best_match.transaction_date else None},
                "score": best_score,
            })
        else:
            unmatched_bank.append({
                "id": b.id,
                "concept": b.concept,
                "amount": b.amount,
                "date": b.transaction_date.isoformat() if b.transaction_date else None,
            })

    for p in prov_txs:
        if p.id not in used_prov:
            unmatched_provider.append({
                "id": p.id,
                "provider_name": p.provider_name,
                "concept": p.concept,
                "amount": p.amount,
                "date": p.transaction_date.isoformat() if p.transaction_date else None,
            })

    await db.commit()

    return {
        "matched": matched,
        "unmatched_bank": unmatched_bank,
        "unmatched_provider": unmatched_provider,
        "summary": {
            "total_bank": len(bank_txs),
            "total_provider": len(prov_txs),
            "matched_count": len(matched),
            "unmatched_bank_count": len(unmatched_bank),
            "unmatched_provider_count": len(unmatched_provider),
        }
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
