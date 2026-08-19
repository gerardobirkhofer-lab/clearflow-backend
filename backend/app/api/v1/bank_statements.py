import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import csv
import io
from datetime import datetime

from app.core.database import get_db
from app.models.bank_transaction import BankTransaction
from app.models_orm import Tenant

router = APIRouter()

@router.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # Verify tenant exists
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=400, detail=f"Tenant {tenant_id} does not exist. Please log out and back in.")
    
    content = await file.read()
    filename = file.filename or "unknown"
    
    if not filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files supported. Please export as CSV.")
    
    try:
        decoded = content.decode('utf-8-sig')  # Handle BOM
    except:
        decoded = content.decode('latin-1')
    
    # Detect delimiter by counting ; vs , in first 3 lines
    lines = decoded.split('\n')[:3]
    semi_count = sum(line.count(';') for line in lines)
    comma_count = sum(line.count(',') for line in lines)
    delimiter = ';' if semi_count > comma_count else ','
    
    transactions = []
    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
    
    for row in reader:
        # Clean up keys (strip whitespace, lowercase)
        clean_row = {k.strip().lower() if k else '': v for k, v in row.items()}
        
        concept = _get_field(clean_row, ['concepto', 'descripcion', 'description', 'concept', 'detalle'])
        amount = _parse_amount(_get_field(clean_row, ['importe', 'amount', 'cantidad', 'valor']))
        tx_date = _parse_date(_get_field(clean_row, ['fecha', 'date', 'fecha valor', 'fecha operacion']))
        reference = _get_field(clean_row, ['referencia', 'reference', 'ref', 'numero'])
        balance = _parse_amount(_get_field(clean_row, ['saldo', 'balance']))
        
        if not concept and not amount:
            continue  # Skip empty rows
        
        tx = BankTransaction(
            tenant_id=tenant_id,
            filename=filename,
            concept=concept,
            amount=amount,
            transaction_date=tx_date,
            reference=reference,
            balance=balance,
            raw_data=str(row),
            matched=0,
        )
        transactions.append(tx)
    
    if not transactions:
        raise HTTPException(status_code=400, detail="No valid transactions found in the file. Check the column headers.")
    
    for tx in transactions:
        db.add(tx)
    
    await db.commit()
    
    return {
        "message": f"Successfully processed {len(transactions)} transactions",
        "count": len(transactions)
    }

def _get_field(row: dict, keys: list) -> str:
    for key in keys:
        if key in row and row[key]:
            return str(row[key]).strip()
    return ''

def _parse_amount(val):
    if not val:
        return 0.0
    val = str(val).replace('.', '').replace(',', '.').replace('€', '').replace('$', '').replace('+', '').strip()
    try:
        return float(val)
    except:
        return 0.0

def _parse_date(val):
    if not val:
        return None
    val = str(val).strip()
    formats = ['%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%m/%d/%Y', '%d/%m/%y']
    for fmt in formats:
        try:
            return datetime.strptime(val, fmt)
        except:
            continue
    return None

@router.get("/")
async def list_transactions(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id)
        .order_by(BankTransaction.transaction_date.desc())
    )
    txs = result.scalars().all()
    return {
        "transactions": [
            {
                "id": t.id,
                "concept": t.concept,
                "amount": t.amount,
                "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
                "reference": t.reference,
                "filename": t.filename,
                "matched": t.matched,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txs
        ],
        "total_count": len(txs),
        "total_amount": sum(t.amount for t in txs),
    }

@router.get("/dashboard")
async def get_dashboard(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    
    # Bank transactions
    bank_result = await db.execute(
        select(BankTransaction).where(BankTransaction.tenant_id == tenant_id)
    )
    bank_txs = bank_result.scalars().all()
    
    # Provider transactions  
    from app.models.provider_transaction import ProviderTransaction
    prov_result = await db.execute(
        select(ProviderTransaction).where(ProviderTransaction.tenant_id == tenant_id)
    )
    prov_txs = prov_result.scalars().all()
    
    # Calculate totals
    total_bank = sum(b.amount for b in bank_txs)
    total_provider = sum(p.amount for p in prov_txs)
    matched_bank = sum(b.amount for b in bank_txs if b.matched)
    pending_bank = sum(b.amount for b in bank_txs if not b.matched)
    pending_provider = sum(p.amount for p in prov_txs if not p.matched)
    
    # Recent activity (last 10)
    recent_bank = sorted(
        [{"id": b.id, "concept": b.concept, "amount": b.amount, "date": b.transaction_date.isoformat() if b.transaction_date else None, "matched": b.matched, "type": "bank"} for b in bank_txs],
        key=lambda x: x["date"] or "",
        reverse=True
    )[:10]
    
    recent_provider = sorted(
        [{"id": p.id, "concept": p.concept, "amount": p.amount, "date": p.transaction_date.isoformat() if p.transaction_date else None, "matched": p.matched, "type": "provider", "provider_name": p.provider_name} for p in prov_txs],
        key=lambda x: x["date"] or "",
        reverse=True
    )[:10]
    
    return {
        "summary": {
            "total_collected": total_bank,
            "total_sales": total_provider,
            "matched_amount": matched_bank,
            "missing_amount": abs(pending_provider) + abs(pending_bank),
            "collection_rate": (matched_bank / total_provider * 100) if total_provider else 0,
            "bank_count": len(bank_txs),
            "provider_count": len(prov_txs),
            "matched_count": sum(1 for b in bank_txs if b.matched),
            "pending_count": sum(1 for b in bank_txs if not b.matched) + sum(1 for p in prov_txs if not p.matched),
        },
        "recent_activity": recent_bank + recent_provider,
        "discrepancies": {
            "unmatched_bank": [{"id": b.id, "concept": b.concept, "amount": b.amount, "date": b.transaction_date.isoformat() if b.transaction_date else None} for b in bank_txs if not b.matched],
            "unmatched_provider": [{"id": p.id, "concept": p.concept, "amount": p.amount, "date": p.transaction_date.isoformat() if p.transaction_date else None, "provider_name": p.provider_name} for p in prov_txs if not p.matched],
        }
    }
