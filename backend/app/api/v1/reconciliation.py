import uuid
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import timedelta
from collections import defaultdict

from app.core.database import get_db
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

router = APIRouter()


def _date_key(dt):
    """Normalize date to date object for indexing."""
    if dt is None:
        return None
    return dt.date() if hasattr(dt, 'date') else dt


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

    # ── OPTIMIZATION: Build indexes ──
    # Index provider transactions by date for fast range lookup
    prov_by_date = defaultdict(list)
    for p in prov_txs:
        dk = _date_key(p.transaction_date)
        if dk:
            prov_by_date[dk].append(p)

    # Index provider transactions by rounded amount (±0.50 EUR buckets)
    prov_by_amount = defaultdict(list)
    for p in prov_txs:
        bucket = round(float(p.amount), 0) if p.amount is not None else 0
        prov_by_amount[bucket].append(p)
        # Also index adjacent buckets for tolerance
        prov_by_amount[bucket + 1].append(p)
        prov_by_amount[bucket - 1].append(p)

    matched = []
    unmatched_bank = []
    unmatched_provider = []

    used_bank = set()
    used_prov = set()

    for b in bank_txs:
        b_date = _date_key(b.transaction_date)
        b_amount_bucket = round(float(b.amount), 0) if b.amount is not None else 0

        # Gather candidates: same date ±3 days AND similar amount
        candidates = set()

        # Date candidates (±3 days)
        if b_date:
            for delta in range(-3, 4):
                check_date = b_date + timedelta(days=delta)
                for p in prov_by_date.get(check_date, []):
                    if p.id not in used_prov:
                        candidates.add(p)

        # Amount candidates (fallback if no date candidates)
        if not candidates:
            for p in prov_by_amount.get(b_amount_bucket, []):
                if p.id not in used_prov:
                    candidates.add(p)
            for p in prov_by_amount.get(b_amount_bucket + 1, []):
                if p.id not in used_prov:
                    candidates.add(p)
            for p in prov_by_amount.get(b_amount_bucket - 1, []):
                if p.id not in used_prov:
                    candidates.add(p)

        best_match = None
        best_score = 0

        for p in candidates:
            score = 0

            # Amount match (exact = 3 points, within 1 cent = 2 points, within 1 EUR = 1 point)
            if b.amount is not None and p.amount is not None:
                amt_diff = abs(b.amount - p.amount)
                if amt_diff < 0.01:
                    score += 3
                elif amt_diff < 1:
                    score += 2
                elif amt_diff < 5:
                    score += 1

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

            # Concept fuzzy match (simple substring)
            if b.concept and p.concept:
                b_conc = b.concept.lower()
                p_conc = p.concept.lower()
                if b_conc in p_conc or p_conc in b_conc:
                    score += 1

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
