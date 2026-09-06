import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import csv
import io
from datetime import datetime

from app.core.database import get_db
from app.models.bank_transaction import BankTransaction
from app.models_orm import Tenant

router = APIRouter()

BATCH_SIZE = 500  # Commit every N transactions to avoid memory/time issues

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
    
    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
    
    batch = []
    total_count = 0
    
    for row in reader:
        # Clean up keys (strip whitespace, lowercase)
        clean_row = {k.strip().lower() if k else '': v for k, v in row.items()}
        
        concept = _get_field(clean_row, ['concepto', 'descripcion', 'description', 'concept', 'detalle'])
        amount = _parse_amount(_get_field(clean_row, ['movimiento', 'importe', 'amount', 'cantidad', 'valor']))
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
        batch.append(tx)
        total_count += 1
        
        # Flush batch to DB every BATCH_SIZE to avoid memory issues and long transactions
        if len(batch) >= BATCH_SIZE:
            db.add_all(batch)
            await db.commit()
            batch = []
    
    # Flush remaining batch
    if batch:
        db.add_all(batch)
        await db.commit()
    
    if total_count == 0:
        raise HTTPException(status_code=400, detail="No valid transactions found in the file. Check the column headers.")
    
    # Auto-trigger reconciliation after upload (non-blocking, ignore errors)
    try:
        from app.services.reconciliation_service import ReconciliationService
        service = ReconciliationService(db, tenant_id)
        await service.run_reconciliation()
    except Exception:
        pass
    
    return {
        "message": f"Successfully processed {total_count} transactions",
        "count": total_count
    }

def _get_field(row: dict, keys: list) -> str:
    for key in keys:
        if key in row and row[key]:
            return str(row[key]).strip()
    return ''

def _parse_amount(val):
    """
    Parsea montos de forma inteligente, auto-detectando el formato.
    Soporta: 1.234,56 (ES) | 1,234.56 (US) | 1234.56 | 1234,56
    """
    if not val:
        return 0.0
    
    val = str(val).replace('€', '').replace('$', '').replace('+', '').strip()
    
    # Caso: tiene ambos separadores (miles y decimales)
    if ',' in val and '.' in val:
        last_comma = val.rfind(',')
        last_dot = val.rfind('.')
        
        if last_comma > last_dot:
            # Formato ES: 1.234,56 → 1234.56
            val = val.replace('.', '').replace(',', '.')
        else:
            # Formato US: 1,234.56 → 1234.56
            val = val.replace(',', '')
    
    # Caso: solo tiene coma
    elif ',' in val:
        # Puede ser decimal (ES: 1234,56) o miles (US: 1,234)
        # Si hay solo una coma y 1-2 dígitos después → decimal
        parts = val.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isdigit():
            val = val.replace(',', '.')
        else:
            # Es separador de miles
            val = val.replace(',', '')
    
    # Caso: solo tiene punto
    # Ya está en formato correcto (1234.56)
    
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
    from app.models.provider_transaction import ProviderTransaction
    
    # Use SQL aggregation instead of loading all transactions into memory
    # Bank aggregates
    bank_agg = await db.execute(
        select(
            func.count(BankTransaction.id).label("count"),
            func.sum(BankTransaction.amount).label("total"),
            func.sum(func.case((BankTransaction.matched == 1, BankTransaction.amount), else_=0)).label("matched"),
            func.sum(func.case((BankTransaction.matched == 0, BankTransaction.amount), else_=0)).label("pending"),
            func.sum(func.case((BankTransaction.matched == 1, 1), else_=0)).label("matched_count"),
            func.sum(func.case((BankTransaction.matched == 0, 1), else_=0)).label("pending_count"),
        ).where(BankTransaction.tenant_id == tenant_id)
    )
    bank_row = bank_agg.one()
    
    # Provider aggregates
    prov_agg = await db.execute(
        select(
            func.count(ProviderTransaction.id).label("count"),
            func.sum(ProviderTransaction.amount).label("total"),
            func.sum(func.case((ProviderTransaction.matched == 1, ProviderTransaction.amount), else_=0)).label("matched"),
            func.sum(func.case((ProviderTransaction.matched == 0, ProviderTransaction.amount), else_=0)).label("pending"),
        ).where(ProviderTransaction.tenant_id == tenant_id)
    )
    prov_row = prov_agg.one()
    
    total_bank = float(bank_row.total or 0)
    total_provider = float(prov_row.total or 0)
    matched_bank = float(bank_row.matched or 0)
    pending_bank = float(bank_row.pending or 0)
    pending_provider = float(prov_row.pending or 0)
    
    # Recent activity (last 10 from each) — still need to load a small subset
    recent_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(10)
    )
    recent_bank = [
        {"id": b.id, "concept": b.concept, "amount": b.amount, "date": b.transaction_date.isoformat() if b.transaction_date else None, "matched": b.matched, "type": "bank"}
        for b in recent_bank_result.scalars().all()
    ]
    
    recent_provider_result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id)
        .order_by(ProviderTransaction.transaction_date.desc())
        .limit(10)
    )
    recent_provider = [
        {"id": p.id, "concept": p.concept, "amount": p.amount, "date": p.transaction_date.isoformat() if p.transaction_date else None, "matched": p.matched, "type": "provider", "provider_name": p.provider_name}
        for p in recent_provider_result.scalars().all()
    ]
    
    # Unmatched for discrepancies section (limit to 50 to avoid huge payloads)
    unmatched_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id, BankTransaction.matched == 0)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(50)
    )
    unmatched_provider_result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id, ProviderTransaction.matched == 0)
        .order_by(ProviderTransaction.transaction_date.desc())
        .limit(50)
    )
    
    return {
        "summary": {
            "total_collected": total_bank,
            "total_sales": total_provider,
            "matched_amount": matched_bank,
            "missing_amount": abs(pending_provider) + abs(pending_bank),
            "collection_rate": (matched_bank / total_provider * 100) if total_provider else 0,
            "bank_count": bank_row.count or 0,
            "provider_count": prov_row.count or 0,
            "matched_count": bank_row.matched_count or 0,
            "pending_count": (bank_row.pending_count or 0) + (prov_row.count or 0) - (prov_row.matched or 0),
            # Aliases for frontend compatibility
            "bank_transactions": bank_row.count or 0,
            "provider_transactions": prov_row.count or 0,
            "matched_bank": matched_bank,
            "matched_provider": matched_bank,
            "pending_bank": pending_bank,
            "pending_provider": pending_provider,
        },
        "recent_activity": recent_bank + recent_provider,
        "discrepancies": {
            "unmatched_bank": [
                {"id": b.id, "concept": b.concept, "amount": b.amount, "date": b.transaction_date.isoformat() if b.transaction_date else None}
                for b in unmatched_bank_result.scalars().all()
            ],
            "unmatched_provider": [
                {"id": p.id, "concept": p.concept, "amount": p.amount, "date": p.transaction_date.isoformat() if p.transaction_date else None, "provider_name": p.provider_name}
                for p in unmatched_provider_result.scalars().all()
            ],
        }
    }
