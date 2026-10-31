import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import csv
import io
from datetime import datetime

from app.core.database import get_shared_db
from app.models.bank_transaction import BankTransaction
from app.models_orm import Tenant

router = APIRouter()

BATCH_SIZE = 500  # Commit every N transactions to avoid memory/time issues


def _tx_source(filename: str | None, bank_name: str | None = None) -> str:
    """Label how the movement entered ClearFlow (Open Banking vs CSV upload)."""
    if (filename or "").lower() in ("open_banking", "open-banking", "tink", "gocardless", "truelayer"):
        return "open_banking"
    if filename:
        return "csv"
    if bank_name:
        return "open_banking"
    return "unknown"


@router.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    db: AsyncSession = Depends(get_shared_db)
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
    
    # Auto-trigger reconciliation after upload — never swallow failures silently
    reconciliation: dict | None = None
    try:
        from app.services.reconciliation_service import ReconciliationService
        service = ReconciliationService(db, tenant_id)
        recon_result = await service.run_reconciliation()
        reconciliation = {
            "status": "ok",
            "result": recon_result if isinstance(recon_result, dict) else {"raw": str(recon_result)},
        }
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception(
            "auto_reconciliation_failed tenant_id=%s after bank upload", tenant_id
        )
        reconciliation = {
            "status": "error",
            "detail": str(exc) or "La conciliación automática falló tras el upload",
        }

    return {
        "message": f"Successfully processed {total_count} transactions",
        "count": total_count,
        "reconciliation": reconciliation,
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
async def list_transactions(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    matched: int | None = Query(
        default=None,
        ge=0,
        le=1,
        description="Filter by match status: 0=unreconciled, 1=reconciled, omit=all",
    ),
):
    """Paginated bank movements. Default limit=50 keeps Payment Check under ~1s."""
    filters = [BankTransaction.tenant_id == tenant_id]
    if matched is not None:
        filters.append(BankTransaction.matched == matched)

    total_count = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(*filters)
    ) or 0
    open_banking_count = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.filename == "open_banking",
        )
    ) or 0
    total_amount = float(
        await db.scalar(
            select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(*filters)
        )
        or 0
    )

    result = await db.execute(
        select(BankTransaction)
        .where(*filters)
        .order_by(BankTransaction.transaction_date.desc())
        .offset(offset)
        .limit(limit)
    )
    txs = result.scalars().all()
    return {
        "transactions": [
            {
                "id": t.id,
                "concept": t.concept,
                "amount": t.amount,
                "transaction_date": t.transaction_date.isoformat() if t.transaction_date else None,
                "date": t.transaction_date.isoformat() if t.transaction_date else None,
                "reference": t.reference,
                "filename": t.filename,
                "bank_name": t.bank_name,
                "source": _tx_source(t.filename, t.bank_name),
                "matched": bool(t.matched),
                "type": "bank",
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txs
        ],
        "total_count": total_count,
        "returned_count": len(txs),
        "limit": limit,
        "offset": offset,
        "matched_filter": matched,
        "total_amount": total_amount,
        "open_banking_count": open_banking_count,
    }

@router.get("/dashboard")
async def get_dashboard(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_shared_db)):
    from app.models.provider_transaction import ProviderTransaction

    bank_count = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(BankTransaction.tenant_id == tenant_id)
    ) or 0
    matched_count = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 1,
        )
    ) or 0
    pending_count_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    ) or 0
    total_bank = float(
        await db.scalar(
            select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
                BankTransaction.tenant_id == tenant_id
            )
        )
        or 0
    )
    matched_bank = float(
        await db.scalar(
            select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.matched == 1,
            )
        )
        or 0
    )
    pending_bank = float(
        await db.scalar(
            select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.matched == 0,
            )
        )
        or 0
    )
    open_banking_count = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.filename == "open_banking",
        )
    ) or 0

    provider_count = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id
        )
    ) or 0
    total_provider = float(
        await db.scalar(
            select(func.coalesce(func.sum(ProviderTransaction.amount), 0)).where(
                ProviderTransaction.tenant_id == tenant_id
            )
        )
        or 0
    )
    pending_provider = float(
        await db.scalar(
            select(func.coalesce(func.sum(ProviderTransaction.amount), 0)).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.matched == 0,
            )
        )
        or 0
    )
    matched_provider_count = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 1,
        )
    ) or 0

    recent_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(25)
    )
    recent_bank = [
        {
            "id": b.id,
            "concept": b.concept,
            "amount": b.amount,
            "date": b.transaction_date.isoformat() if b.transaction_date else None,
            "matched": bool(b.matched),
            "type": "bank",
            "source": _tx_source(b.filename, b.bank_name),
            "bank_name": b.bank_name,
            "filename": b.filename,
        }
        for b in recent_bank_result.scalars().all()
    ]

    recent_provider_result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id)
        .order_by(ProviderTransaction.transaction_date.desc())
        .limit(10)
    )
    recent_provider = [
        {
            "id": p.id,
            "concept": p.concept,
            "amount": p.amount,
            "date": p.transaction_date.isoformat() if p.transaction_date else None,
            "matched": bool(p.matched),
            "type": "provider",
            "source": "provider",
            "provider_name": p.provider_name,
        }
        for p in recent_provider_result.scalars().all()
    ]

    unmatched_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id, BankTransaction.matched == 0)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(100)
    )
    matched_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id, BankTransaction.matched == 1)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(50)
    )
    unmatched_provider_result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id, ProviderTransaction.matched == 0)
        .order_by(ProviderTransaction.transaction_date.desc())
        .limit(50)
    )

    def _bank_row(b: BankTransaction, matched: bool) -> dict:
        return {
            "id": b.id,
            "concept": b.concept,
            "amount": b.amount,
            "date": b.transaction_date.isoformat() if b.transaction_date else None,
            "matched": matched,
            "type": "bank",
            "source": _tx_source(b.filename, b.bank_name),
            "bank_name": b.bank_name,
        }

    return {
        "summary": {
            "total_collected": total_bank,
            "total_sales": total_provider,
            "matched_amount": matched_bank,
            "missing_amount": abs(pending_provider) + abs(pending_bank),
            "collection_rate": (matched_bank / total_provider * 100) if total_provider else 0,
            "bank_count": bank_count,
            "provider_count": provider_count,
            "matched_count": matched_count,
            "pending_count": pending_count_bank + max(0, provider_count - matched_provider_count),
            "bank_transactions": bank_count,
            "provider_transactions": provider_count,
            "open_banking_transactions": open_banking_count,
            "matched_bank": matched_bank,
            "matched_provider": matched_bank,
            "pending_bank": pending_bank,
            "pending_provider": pending_provider,
        },
        "recent_activity": recent_bank + recent_provider,
        "discrepancies": {
            "unmatched_bank": [_bank_row(b, False) for b in unmatched_bank_result.scalars().all()],
            "matched_bank": [_bank_row(b, True) for b in matched_bank_result.scalars().all()],
            "unmatched_provider": [
                {
                    "id": p.id,
                    "concept": p.concept,
                    "amount": p.amount,
                    "date": p.transaction_date.isoformat() if p.transaction_date else None,
                    "matched": False,
                    "type": "provider",
                    "source": "provider",
                    "provider_name": p.provider_name,
                }
                for p in unmatched_provider_result.scalars().all()
            ],
        },
    }
