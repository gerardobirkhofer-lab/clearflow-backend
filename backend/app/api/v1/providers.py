import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import csv
import io
from datetime import datetime

from app.core.database import get_db
from app.models.provider_transaction import ProviderTransaction
from app.models.tenant import Tenant

router = APIRouter()

@router.post("/upload")
async def upload_provider_report(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    provider_name: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant not found")

    content = await file.read()
    filename = file.filename or "unknown"

    if not filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files supported")

    try:
        decoded = content.decode('utf-8-sig')
    except:
        decoded = content.decode('latin-1')

    lines = decoded.split('\n')[:3]
    semi_count = sum(line.count(';') for line in lines)
    delimiter = ';' if semi_count > sum(line.count(',') for line in lines) else ','

    transactions = []
    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)

    for row in reader:
        clean_row = {k.strip().lower() if k else '': v for k, v in row.items()}
        concept = _get_field(clean_row, ['concepto', 'descripcion', 'description', 'concept', 'detalle', 'tipo'])
        amount = _parse_amount(_get_field(clean_row, ['importe', 'amount', 'cantidad', 'valor', 'total']))
        tx_date = _parse_date(_get_field(clean_row, ['fecha', 'date', 'fecha valor', 'fecha operacion']))
        reference = _get_field(clean_row, ['referencia', 'reference', 'ref', 'numero', 'id'])

        if not concept and not amount:
            continue

        tx = ProviderTransaction(
            tenant_id=tenant_id,
            provider_name=provider_name,
            filename=filename,
            concept=concept,
            amount=amount,
            transaction_date=tx_date,
            reference=reference,
            raw_data=str(row),
            matched=0,
        )
        transactions.append(tx)

    if not transactions:
        raise HTTPException(status_code=400, detail="No valid transactions found")

    for tx in transactions:
        db.add(tx)

    await db.commit()

    return {
        "message": f"Successfully processed {len(transactions)} {provider_name} transactions",
        "count": len(transactions)
    }

@router.get("/")
async def list_provider_transactions(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id)
        .order_by(ProviderTransaction.transaction_date.desc())
    )
    txs = result.scalars().all()
    return {
        "transactions": [
            {
                "id": t.id,
                "provider_name": t.provider_name,
                "concept": t.concept,
                "amount": t.amount,
                "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
                "reference": t.reference,
                "filename": t.filename,
                "matched": t.matched,
            }
            for t in txs
        ]
    }

def _get_field(row: dict, keys: list) -> str:
    for key in keys:
        if key in row and row[key]:
            return row[key]
    row_lower = {k.lower().strip(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in row_lower and row_lower[key.lower()]:
            return row_lower[key.lower()]
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
