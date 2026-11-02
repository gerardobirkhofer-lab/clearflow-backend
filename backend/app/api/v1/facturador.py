"""
Facturador intake — sales / ticket gross lines that feed Reconstruct
when the liquidator pack is opaque or incomplete.

Doctrine key: "facturador" (alongside banco, wallet, contrato).
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant import assert_tenant_access
from app.models.provider_transaction import ProviderTransaction
from app.services.ingest_utils import fingerprint

router = APIRouter()

FACTURADOR_KIND = "facturador_sale"
ENGINE = "facturador-intake-v1"


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace("€", "").strip())
    except ValueError:
        return None


@router.get("/doctrine")
async def facturador_doctrine():
    return {
        "engine": ENGINE,
        "key": "facturador",
        "purpose": (
            "Ingest ticket / POS / ERP gross sales so Reconstruct can rebuild "
            "expected net from the signed fee schedule when the acquirer is opaque."
        ),
        "accepted": {
            "csv_columns": ["date", "gross", "ticket_id?", "concept?", "net?"],
            "json": {"lines": [{"date": "YYYY-MM-DD", "gross": 12.5, "ticket_id": "..."}]},
        },
        "tx_kind": FACTURADOR_KIND,
        "note": "Does not auto-confirm. Feeds control Reconstruct + contractual math.",
    }


@router.get("/status")
async def facturador_status(
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    total = (
        await db.execute(
            select(func.count())
            .select_from(ProviderTransaction)
            .where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.tx_kind == FACTURADOR_KIND,
            )
        )
    ).scalar_one()
    return {
        "engine": ENGINE,
        "tenant_id": str(tenant_id),
        "lines": int(total or 0),
        "tx_kind": FACTURADOR_KIND,
    }


@router.post("/intake")
async def facturador_intake(
    file: UploadFile | None = File(None),
    tenant_id: uuid.UUID = Form(...),
    source_name: str = Form("facturador"),
    payload_json: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Accept CSV or JSON lines of sales gross.
    Idempotent via ingest_fingerprint. Stores as tx_kind=facturador_sale.
    """
    tenant_id = assert_tenant_access(tenant_id, current_user)
    rows: list[dict] = []
    filename = "json_payload"

    if file and file.filename:
        filename = file.filename
        raw = (await file.read()).decode("utf-8-sig", errors="replace")
        lower = filename.lower()
        if lower.endswith(".json"):
            data = json.loads(raw)
            rows = data.get("lines") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise HTTPException(400, "JSON must be a list or {lines: [...]}")
        else:
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)
    elif payload_json:
        data = json.loads(payload_json)
        rows = data.get("lines") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise HTTPException(400, "payload_json must be a list or {lines: [...]}")
    else:
        raise HTTPException(400, "Provide file (CSV/JSON) or payload_json")

    inserted = 0
    skipped = 0
    errors: list[dict] = []

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({"row": i + 1, "error": "not an object"})
            continue
        # column aliases
        gross = _float(
            row.get("gross")
            or row.get("bruto")
            or row.get("importe")
            or row.get("amount")
            or row.get("total")
        )
        if gross is None or gross <= 0:
            errors.append({"row": i + 1, "error": "missing/invalid gross"})
            continue
        dt = _parse_date(
            str(row.get("date") or row.get("fecha") or row.get("transaction_date") or "")
        )
        ticket = str(
            row.get("ticket_id")
            or row.get("ticket")
            or row.get("reference")
            or row.get("id")
            or f"row-{i+1}"
        )[:255]
        concept = str(row.get("concept") or row.get("concepto") or "Venta facturador")[:500]
        net = _float(row.get("net") or row.get("neto"))
        fp = fingerprint(
            tenant_id,
            channel="facturador",
            reference=ticket,
            amount=gross,
            tx_date=dt,
            concept=concept,
            filename=filename,
            provider_name=source_name,
        )

        exists = (
            await db.execute(
                select(ProviderTransaction.id).where(
                    ProviderTransaction.tenant_id == tenant_id,
                    ProviderTransaction.ingest_fingerprint == fp,
                )
            )
        ).scalar_one_or_none()
        if exists:
            skipped += 1
            continue

        db.add(
            ProviderTransaction(
                tenant_id=tenant_id,
                provider_name=source_name[:100],
                filename=filename[:255],
                concept=concept,
                amount=gross,
                transaction_date=dt or datetime.utcnow(),
                reference=ticket,
                raw_data=json.dumps(row, ensure_ascii=False)[:4000],
                matched=0,
                match_status="pending",
                ingest_fingerprint=fp,
                tx_kind=FACTURADOR_KIND,
                gross_amount=gross,
                net_amount=net,
                contract_gross_basis=gross,
                contract_audit_status="pending_audit",
            )
        )
        inserted += 1

    await db.commit()
    return {
        "engine": ENGINE,
        "tx_kind": FACTURADOR_KIND,
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "errors": errors[:50],
        "error_count": len(errors),
        "note": "Lines ready for Control → Reconstruct. No auto-confirm.",
    }
