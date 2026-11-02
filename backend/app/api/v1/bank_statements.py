import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import csv
import io
from datetime import datetime

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant import assert_tenant_access, resolve_tenant_id
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
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Bank CSV ingest with idempotency + per-row errors.
    CSV remains required as backup / history and when OB consent is down —
    it does not replace Open Banking; it complements it.
    """
    from app.services.ingest_utils import (
        classify_kind,
        extract_fee_fields,
        fingerprint,
        settlement_id_from_row,
    )

    tenant_id = assert_tenant_access(tenant_id, current_user)
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=400, detail=f"Tenant {tenant_id} does not exist. Please log out and back in.")

    content = await file.read()
    filename = file.filename or "unknown"
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files supported. Please export as CSV.")

    try:
        decoded = content.decode("utf-8-sig")
    except Exception:
        decoded = content.decode("latin-1")

    lines = decoded.split("\n")[:3]
    semi_count = sum(line.count(";") for line in lines)
    comma_count = sum(line.count(",") for line in lines)
    delimiter = ";" if semi_count > comma_count else ","

    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
    headers = [((h or "").strip().lower()) for h in (reader.fieldnames or [])]

    batch = []
    inserted = 0
    skipped_dupes = 0
    rejected_rows: list[dict] = []
    seen_fps: set[str] = set()
    line_no = 1  # header is line 1

    for row in reader:
        line_no += 1
        clean_row = {k.strip().lower() if k else "": v for k, v in row.items()}
        concept = _get_field(clean_row, ["concepto", "descripcion", "description", "concept", "detalle"])
        amount = _parse_amount(_get_field(clean_row, ["movimiento", "importe", "amount", "cantidad", "valor"]))
        tx_date = _parse_date(_get_field(clean_row, ["fecha", "date", "fecha valor", "fecha operacion"]))
        reference = _get_field(clean_row, ["referencia", "reference", "ref", "numero"])
        balance = _parse_amount(_get_field(clean_row, ["saldo", "balance"]))
        fees = extract_fee_fields(clean_row, _parse_amount)
        settlement_id = settlement_id_from_row(clean_row)

        if not concept and amount == 0.0 and not reference:
            rejected_rows.append({"line": line_no, "reason": "empty_row"})
            continue
        if amount == 0.0 and not _get_field(clean_row, ["movimiento", "importe", "amount", "cantidad", "valor"]):
            rejected_rows.append({"line": line_no, "reason": "missing_amount", "concept": concept[:80]})
            continue
        if tx_date is None and _get_field(clean_row, ["fecha", "date", "fecha valor", "fecha operacion"]):
            rejected_rows.append({"line": line_no, "reason": "unparseable_date", "concept": concept[:80]})

        fp = fingerprint(
            tenant_id,
            channel="bank_csv",
            reference=reference or None,
            amount=amount,
            tx_date=tx_date,
            concept=concept,
            filename=filename,
        )
        if fp in seen_fps:
            skipped_dupes += 1
            continue
        seen_fps.add(fp)

        existing = await db.execute(
            select(BankTransaction.id).where(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.ingest_fingerprint == fp,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            skipped_dupes += 1
            continue

        kind = classify_kind(channel="bank_csv", filename=filename, concept=concept, headers=headers)
        primary_amount = amount
        if fees.get("net_amount") is not None and abs(fees["net_amount"] or 0) > 0:
            # Prefer net for bank-side settlement credits when provided
            primary_amount = fees["net_amount"]  # type: ignore[assignment]

        tx = BankTransaction(
            tenant_id=tenant_id,
            filename=filename,
            concept=concept,
            amount=primary_amount,
            transaction_date=tx_date,
            reference=reference or None,
            balance=balance if balance else None,
            raw_data=str(row),
            matched=0,
            match_status="pending",
            ingest_fingerprint=fp,
            tx_kind=kind,
            gross_amount=fees.get("gross_amount"),
            fee_amount=fees.get("fee_amount"),
            net_amount=fees.get("net_amount"),
            settlement_id=settlement_id,
        )
        batch.append(tx)
        inserted += 1

        if len(batch) >= BATCH_SIZE:
            db.add_all(batch)
            await db.commit()
            batch = []

    if batch:
        db.add_all(batch)
        await db.commit()

    if inserted == 0 and skipped_dupes == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "No valid transactions found in the file. Check the column headers.",
                "rejected_rows": rejected_rows[:50],
            },
        )

    reconciliation: dict | None = None
    try:
        from app.api.v1.reconciliation import execute_reconciliation
        recon_result = await execute_reconciliation(db, tenant_id)
        reconciliation = {"status": "ok", "result": recon_result}
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
        "message": f"Inserted {inserted} bank rows ({skipped_dupes} duplicates skipped)",
        "count": inserted,
        "inserted": inserted,
        "skipped_duplicates": skipped_dupes,
        "rejected_rows": rejected_rows[:100],
        "rejected_count": len(rejected_rows),
        "reconciliation": reconciliation,
        "channel": "csv_bank",
        "note": "CSV complements Open Banking; it does not replace live bank sync.",
    }


@router.get("/intake-channels")
async def intake_channels(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Product truth: which ports deliver what.
    Open Banking ≠ POS settlement packs — CSV/API reports fill that gap.
    """
    from app.services.ingest_utils import intake_channels_doc

    _ = current_user  # auth required; doc is tenant-agnostic
    return intake_channels_doc()


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
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    matched: int | None = Query(
        default=None,
        ge=0,
        le=1,
        description="Filter by match status: 0=unreconciled, 1=reconciled, omit=all",
    ),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD"),
    match_status: str | None = Query(
        default=None,
        description="pending|suggested|confirmed|user_confirmed|rejected",
    ),
):
    """Paginated bank movements. Default limit=50 keeps Payment Check under ~1s."""
    filters = [BankTransaction.tenant_id == tenant_id]
    if matched is not None:
        filters.append(BankTransaction.matched == matched)
    if match_status:
        filters.append(BankTransaction.match_status == match_status)
    if date_from:
        try:
            filters.append(
                BankTransaction.transaction_date >= datetime.fromisoformat(date_from)
            )
        except ValueError as exc:
            raise HTTPException(422, f"Invalid date_from: {date_from}") from exc
    if date_to:
        try:
            filters.append(
                BankTransaction.transaction_date <= datetime.fromisoformat(date_to)
            )
        except ValueError as exc:
            raise HTTPException(422, f"Invalid date_to: {date_to}") from exc

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
                "match_status": getattr(t, "match_status", None)
                or ("confirmed" if t.matched else "pending"),
                "match_evidence": getattr(t, "match_evidence", None),
                "matched_at": t.matched_at.isoformat() if getattr(t, "matched_at", None) else None,
                "tx_kind": getattr(t, "tx_kind", None) or "bank_movement",
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
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
):
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
    contractual_disc_count = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.match_status == "contractual_discrepancy",
        )
    ) or 0
    gateway_deviation_count = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.contract_audit_status == "gateway_deviation",
        )
    ) or 0

    recent_bank_result = await db.execute(
        select(BankTransaction)
        .where(BankTransaction.tenant_id == tenant_id)
        .order_by(BankTransaction.transaction_date.desc())
        .limit(25)
    )
    def _status_of(tx) -> str:
        status = getattr(tx, "match_status", None) or ("confirmed" if tx.matched else "pending")
        return status

    recent_bank = [
        {
            "id": b.id,
            "concept": b.concept,
            "amount": b.amount,
            "date": b.transaction_date.isoformat() if b.transaction_date else None,
            "matched": bool(b.matched),
            "match_status": _status_of(b),
            "match_evidence": getattr(b, "match_evidence", None),
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
            "match_status": _status_of(p),
            "match_evidence": getattr(p, "match_evidence", None),
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
            "match_status": _status_of(b),
            "match_evidence": getattr(b, "match_evidence", None),
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
            "confirmed_count": matched_count,
            "pending_count": pending_count_bank + max(0, provider_count - matched_provider_count),
            "bank_transactions": bank_count,
            "provider_transactions": provider_count,
            "open_banking_transactions": open_banking_count,
            "matched_bank": matched_bank,
            "matched_provider": matched_bank,
            "pending_bank": pending_bank,
            "pending_provider": pending_provider,
            "auto_confirm_only": True,
            "engine": "clearflow_v5_contract_audit",
            "contractual_discrepancies": contractual_disc_count,
            "gateway_fee_deviations": gateway_deviation_count,
        },
        "recent_activity": recent_bank + recent_provider,
        "discrepancies": {
            "contractual": contractual_disc_count,
            "gateway_deviations": gateway_deviation_count,
            "unmatched_bank": [_bank_row(b, False) for b in unmatched_bank_result.scalars().all()],
            "matched_bank": [_bank_row(b, True) for b in matched_bank_result.scalars().all()],
            "unmatched_provider": [
                {
                    "id": p.id,
                    "concept": p.concept,
                    "amount": p.amount,
                    "date": p.transaction_date.isoformat() if p.transaction_date else None,
                    "matched": False,
                    "match_status": "pending",
                    "type": "provider",
                    "source": "provider",
                    "provider_name": p.provider_name,
                }
                for p in unmatched_provider_result.scalars().all()
            ],
        },
    }
