import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
import csv
import io
from datetime import datetime

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant import assert_tenant_access
from app.models.provider_transaction import ProviderTransaction
from app.models.provider import Provider
from app.models_orm import Tenant

router = APIRouter()

@router.post("/upload")
async def upload_provider_report(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    provider_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Provider settlement / sale CSV — complementary to Open Banking.
    OB brings bank movements; this brings sale lines + gross/fee/net packs.
    Idempotent via ingest_fingerprint; bad rows reported, not silent-dropped.
    """
    from app.services.ingest_utils import (
        fingerprint,
        classify_kind,
        extract_fee_fields,
        settlement_id_from_row,
    )

    tenant_id = assert_tenant_access(tenant_id, current_user)
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant not found")

    content = await file.read()
    filename = file.filename or "unknown"

    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files supported")

    try:
        decoded = content.decode("utf-8-sig")
    except Exception:
        decoded = content.decode("latin-1")

    lines = decoded.split("\n")[:3]
    semi_count = sum(line.count(";") for line in lines)
    delimiter = ";" if semi_count > sum(line.count(",") for line in lines) else ","

    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
    headers = [h.strip().lower() for h in (reader.fieldnames or []) if h]
    kind = classify_kind(
        channel="provider_csv",
        filename=filename,
        headers=headers,
    )

    from app.services.contract_math import ContractTerms
    from app.services.contractual_audit import apply_contract_recalc

    # Load contract terms for this provider name
    prov_row = await db.execute(
        select(Provider).where(
            Provider.tenant_id == tenant_id,
            or_(
                Provider.name.ilike(provider_name),
                Provider.provider_type.ilike(provider_name),
            ),
        )
    )
    provider_cfg = prov_row.scalar_one_or_none()
    terms = ContractTerms.from_provider(provider_cfg) if provider_cfg else None

    inserted = 0
    skipped_dupes = 0
    rejected_rows: list[dict] = []
    transactions: list[ProviderTransaction] = []
    seen_fps: set[str] = set()
    gateway_deviations = 0

    for line_no, row in enumerate(reader, start=2):
        clean_row = {k.strip().lower() if k else "": v for k, v in row.items()}
        if not any(str(v or "").strip() for v in clean_row.values()):
            rejected_rows.append({"line": line_no, "reason": "empty_row"})
            continue

        concept = _get_field(
            clean_row,
            ["concepto", "descripcion", "description", "concept", "detalle", "tipo"],
        )
        fees = extract_fee_fields(clean_row, _parse_amount)
        # Prefer net for bank-facing amount; fall back to importe/amount
        amount_raw = _get_field(
            clean_row,
            ["liquido", "líquido", "net", "neto", "importe neto", "net_amount", "payout"],
        )
        if not amount_raw:
            amount_raw = _get_field(
                clean_row, ["importe", "amount", "cantidad", "valor", "total", "bruto", "gross"]
            )
        amount = _parse_amount(amount_raw)
        if fees.get("net_amount") is not None and not amount_raw:
            amount = float(fees["net_amount"])

        tx_date = _parse_date(
            _get_field(clean_row, ["fecha", "date", "fecha valor", "fecha operacion", "fecha operación"])
        )
        reference = _get_field(
            clean_row, ["referencia", "reference", "ref", "numero", "número", "id", "order_id"]
        )
        settle_id = settlement_id_from_row(clean_row)

        if not concept and not amount and not reference:
            rejected_rows.append({"line": line_no, "reason": "missing_fields"})
            continue
        if amount == 0.0 and not amount_raw and fees.get("net_amount") is None:
            rejected_rows.append({"line": line_no, "reason": "missing_amount", "concept": (concept or "")[:80]})
            continue

        row_kind = classify_kind(
            channel="provider_csv",
            filename=filename,
            concept=concept,
            headers=headers,
        ) or kind

        fp = fingerprint(
            tenant_id,
            channel="provider_csv",
            reference=reference or settle_id,
            amount=amount,
            tx_date=tx_date,
            concept=concept,
            filename=filename,
            provider_name=provider_name,
        )
        if fp in seen_fps:
            skipped_dupes += 1
            continue
        seen_fps.add(fp)

        existing = await db.execute(
            select(ProviderTransaction.id).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.ingest_fingerprint == fp,
            )
        )
        if existing.scalar_one_or_none() is not None:
            skipped_dupes += 1
            continue

        audit = apply_contract_recalc(
            amount=amount,
            gross_amount=fees.get("gross_amount"),
            fee_amount=fees.get("fee_amount"),
            net_amount=fees.get("net_amount"),
            terms=terms,
            sale_date=tx_date,
        )
        if audit["contract_audit_status"] == "gateway_deviation":
            gateway_deviations += 1

        transactions.append(
            ProviderTransaction(
                tenant_id=tenant_id,
                provider_name=provider_name,
                filename=filename,
                concept=concept,
                amount=amount,
                transaction_date=tx_date,
                reference=reference or None,
                raw_data=str(row),
                matched=0,
                match_status="pending",
                ingest_fingerprint=fp,
                tx_kind=row_kind,
                gross_amount=fees.get("gross_amount"),
                fee_amount=fees.get("fee_amount"),
                net_amount=fees.get("net_amount"),
                settlement_id=settle_id,
                gateway_reported_net=audit["gateway_reported_net"],
                contract_gross_basis=audit["contract_gross_basis"],
                contract_expected_fee=audit["contract_expected_fee"],
                contract_expected_net=audit["contract_expected_net"],
                contract_audit_status=audit["contract_audit_status"],
                contract_audit_json=audit["contract_audit_json"],
            )
        )
        inserted += 1

    if not transactions and skipped_dupes == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "No valid transactions found",
                "rejected_rows": rejected_rows[:50],
                "rejected_count": len(rejected_rows),
            },
        )

    for tx in transactions:
        db.add(tx)
    await db.commit()

    return {
        "message": f"Processed {provider_name}: {inserted} new, {skipped_dupes} duplicates skipped",
        "count": inserted,
        "skipped_duplicates": skipped_dupes,
        "rejected_rows": rejected_rows[:100],
        "rejected_count": len(rejected_rows),
        "tx_kind": kind,
        "channel": "provider_csv",
        "contract_audit": {
            "terms_loaded": terms is not None,
            "gateway_deviations": gateway_deviations,
            "note": (
                "Neto esperado recalculado desde contrato; "
                "no se confía ciegamente en el neto de la pasarela."
            ),
        },
    }

@router.get("/")
async def list_provider_transactions(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    limit: int = 200,
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    limit = max(1, min(int(limit or 200), 1000))
    result = await db.execute(
        select(ProviderTransaction)
        .where(ProviderTransaction.tenant_id == tenant_id)
        .order_by(ProviderTransaction.transaction_date.desc())
        .limit(limit)
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
                "matched": bool(t.matched),
                "match_status": getattr(t, "match_status", None)
                or ("confirmed" if t.matched else "pending"),
                "match_evidence": getattr(t, "match_evidence", None),
                "tx_kind": getattr(t, "tx_kind", None),
                "gross_amount": getattr(t, "gross_amount", None),
                "fee_amount": getattr(t, "fee_amount", None),
                "net_amount": getattr(t, "net_amount", None),
                "settlement_id": getattr(t, "settlement_id", None),
            }
            for t in txs
        ],
        "returned_count": len(txs),
        "limit": limit,
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
        parts = val.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isdigit():
            val = val.replace(',', '.')
        else:
            val = val.replace(',', '')
    
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


def _provider_public(p: Provider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "settlement_mode": p.settlement_mode,
        "credit_delay_days": p.credit_delay_days,
        "fee_percent": p.fee_percent,
        "fee_fixed": p.fee_fixed,
        "monthly_fee": p.monthly_fee,
        "has_iva": bool(getattr(p, "has_iva", 0)),
        "iva_percent": getattr(p, "iva_percent", 21.0),
        "payout_destination": getattr(p, "payout_destination", None) or "bank",
        "dispute_email": p.dispute_email,
        "contract_file_url": p.contract_file_url,
        "is_active": p.is_active,
    }


@router.get("/list")
async def list_providers(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all providers configured for a tenant (includes contract terms)."""
    tenant_id = assert_tenant_access(tenant_id, current_user)
    result = await db.execute(
        select(Provider).where(Provider.tenant_id == tenant_id)
    )
    providers = result.scalars().all()
    return {"providers": [_provider_public(p) for p in providers]}


@router.post("/contracts/sync")
async def sync_provider_contracts(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Upsert liquidadora contract terms from Setup (source of truth for Mode B).
    Body: { tenant_id, providers: [ { name, fee_percent, fee_fixed, ... } ] }
    """
    from uuid import UUID as _UUID

    raw_tid = payload.get("tenant_id")
    tenant_id = assert_tenant_access(
        _UUID(str(raw_tid)) if raw_tid else None,
        current_user,
    )
    items = payload.get("providers") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="providers[] required")

    upserted = []
    for item in items:
        name = (item.get("name") or item.get("id") or "").strip()
        if not name:
            continue
        existing = await db.execute(
            select(Provider).where(
                Provider.tenant_id == tenant_id,
                Provider.name == name,
            )
        )
        p = existing.scalar_one_or_none()
        if not p:
            p = Provider(
                tenant_id=tenant_id,
                name=name,
                provider_type=(item.get("provider_type") or item.get("id") or name).lower(),
            )
            db.add(p)

        if item.get("fee_percent") is not None and str(item.get("fee_percent")) != "":
            p.fee_percent = float(item["fee_percent"])
        if item.get("fee_fixed") is not None and str(item.get("fee_fixed")) != "":
            p.fee_fixed = float(item["fee_fixed"])
        if item.get("monthly_fee") is not None and str(item.get("monthly_fee")) != "":
            p.monthly_fee = float(item["monthly_fee"])
        if "has_iva" in item:
            p.has_iva = 1 if item.get("has_iva") else 0
        if item.get("iva_percent") is not None and str(item.get("iva_percent")) != "":
            p.iva_percent = float(item["iva_percent"])
        days = item.get("payout_days") or item.get("credit_delay_days")
        if days is not None and str(days) != "":
            p.credit_delay_days = int(float(days))
        dest = item.get("payout_destination")
        if item.get("is_virtual_account"):
            dest = "wallet"
        if dest:
            p.payout_destination = str(dest)
        if item.get("email") or item.get("dispute_email"):
            p.dispute_email = item.get("dispute_email") or item.get("email")
        if item.get("contract_file_url"):
            p.contract_file_url = item["contract_file_url"]
        p.is_active = 1
        upserted.append(name)

    await db.commit()
    return {
        "message": f"Synced {len(upserted)} contract(s)",
        "providers": upserted,
        "note": "Estos términos alimentan Strategy Reconstruct cuando no hay detalle de liquidadora.",
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
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update provider settings (dispute email, fees, etc.)."""
    tenant_id = assert_tenant_access(tenant_id, current_user)
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
