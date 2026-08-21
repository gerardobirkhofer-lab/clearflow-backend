import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import csv
import io
from datetime import datetime

from app.core.database import get_db
from app.models.provider_transaction import ProviderTransaction
from app.models.provider import Provider
from app.models_orm import Tenant

router = APIRouter()
from app.models.provider_transaction import ProviderTransaction
from app.models_orm import Tenant

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


@router.get("/list")
async def list_providers(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """List all providers configured for a tenant."""
    result = await db.execute(
        select(Provider).where(Provider.tenant_id == tenant_id)
    )
    providers = result.scalars().all()
    return {
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "provider_type": p.provider_type,
                "settlement_mode": p.settlement_mode,
                "fee_percent": p.fee_percent,
                "fee_fixed": p.fee_fixed,
                "monthly_fee": p.monthly_fee,
                "dispute_email": p.dispute_email,
                "is_active": p.is_active,
            }
            for p in providers
        ]
    }


@router.patch("/{provider_id}")
async def update_provider(
    provider_id: int,
    tenant_id: uuid.UUID,
    dispute_email: str = "",
    fee_percent: float = None,
    fee_fixed: float = None,
    monthly_fee: float = None,
    db: AsyncSession = Depends(get_db),
):
    """Update provider settings (dispute email, fees, etc.)."""
    result = await db.execute(
        select(Provider).where(
            Provider.id == provider_id,
            Provider.tenant_id == tenant_id,
        )
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if dispute_email:
        provider.dispute_email = dispute_email
    if fee_percent is not None:
        provider.fee_percent = fee_percent
    if fee_fixed is not None:
        provider.fee_fixed = fee_fixed
    if monthly_fee is not None:
        provider.monthly_fee = monthly_fee

    await db.commit()
    await db.refresh(provider)
    return {
        "id": provider.id,
        "name": provider.name,
        "dispute_email": provider.dispute_email,
        "message": "Provider updated",
    }
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
