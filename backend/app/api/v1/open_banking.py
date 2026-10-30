"""
Open Banking endpoints (GoCardless Bank Account Data / PSD2).

Flow:
  1. GET  /institutions?country=ES
  2. POST /link  → returns bank authorization URL (~90 day consent)
  3. User approves at the bank
  4. GET  /callback?ref=...  (public) → stores connection
  5. POST /sync/{tenant_id} → pulls transactions into bank_transactions
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_shared_db
from app.models.bank_account import BankAccount
from app.models.bank_transaction import BankTransaction
from app.models.provider_connection import ProviderConnection
from app.models_orm import Tenant
from app.services.open_banking import (
    OpenBankingError,
    get_open_banking_client,
    is_configured,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/open-banking", tags=["open-banking"])

PROVIDER_NAME = "gocardless_bank"
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
API_PUBLIC_URL = os.getenv(
    "API_PUBLIC_URL",
    "https://clearflow-api-production.up.railway.app",
).rstrip("/")


class CreateLinkRequest(BaseModel):
    tenant_id: uuid.UUID
    institution_id: str = Field(..., min_length=2)
    country: str = "ES"
    redirect_after: str | None = None  # where to send the user after callback


def _http_error(exc: OpenBankingError) -> HTTPException:
    code = exc.status_code or 502
    if code == 401:
        code = 502
    return HTTPException(status_code=min(code, 502), detail=str(exc))


@router.get("/status")
async def open_banking_status() -> dict[str, Any]:
    return {
        "ok": True,
        "provider": "gocardless_bank_account_data",
        "configured": is_configured(),
        "access_days_default": int(os.getenv("OPEN_BANKING_ACCESS_DAYS", "90")),
        "history_days_default": int(os.getenv("OPEN_BANKING_HISTORY_DAYS", "90")),
        "callback_url": f"{API_PUBLIC_URL}/api/v1/open-banking/callback",
        "notes": (
            "Set GOCARDLESS_BANK_SECRET_ID and GOCARDLESS_BANK_SECRET_KEY "
            "(or NORDIGEN_SECRET_ID / NORDIGEN_SECRET_KEY) in Railway."
        ),
    }


@router.get("/institutions")
async def list_institutions(country: str = Query(default="ES", min_length=2, max_length=2)):
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Open Banking credentials not configured on the server",
        )
    client = get_open_banking_client()
    try:
        institutions = await client.list_institutions(country=country)
    except OpenBankingError as exc:
        raise _http_error(exc) from exc
    return {"country": country.upper(), "count": len(institutions), "institutions": institutions}


@router.post("/link")
async def create_bank_link(
    body: CreateLinkRequest,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Start Open Banking consent for a tenant + bank.
    Returns { link, requisition_id, agreement_id, access_valid_for_days }.
    """
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Open Banking credentials not configured on the server",
        )

    result = await db.execute(select(Tenant).where(Tenant.id == body.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    client = get_open_banking_client()
    try:
        agreement = await client.create_agreement(body.institution_id)
        agreement_id = agreement["id"]
        access_days = agreement.get("access_valid_for_days") or 90

        # reference encodes tenant + optional frontend return path
        reference = f"cf:{body.tenant_id}:{body.institution_id}"
        callback = f"{API_PUBLIC_URL}/api/v1/open-banking/callback"

        requisition = await client.create_requisition(
            redirect=callback,
            institution_id=body.institution_id,
            agreement_id=agreement_id,
            reference=reference,
            user_language="ES",
        )
    except OpenBankingError as exc:
        raise _http_error(exc) from exc

    requisition_id = requisition.get("id")
    link = requisition.get("link")
    if not requisition_id or not link:
        raise HTTPException(status_code=502, detail="Open Banking did not return a link")

    # Upsert pending connection
    existing = await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == body.tenant_id,
            ProviderConnection.provider_name == PROVIDER_NAME,
            ProviderConnection.account_id == body.institution_id,
        )
    )
    conn = existing.scalar_one_or_none()
    extra = {
        "requisition_id": requisition_id,
        "agreement_id": agreement_id,
        "institution_id": body.institution_id,
        "access_valid_for_days": access_days,
        "redirect_after": body.redirect_after or f"{FRONTEND_URL}/setup",
        "status_detail": "pending_user_authorization",
    }
    if conn:
        conn.status = "pending"
        conn.extra_data = {**(conn.extra_data or {}), **extra}
    else:
        conn = ProviderConnection(
            tenant_id=body.tenant_id,
            provider_name=PROVIDER_NAME,
            account_id=body.institution_id,
            status="pending",
            extra_data=extra,
        )
        db.add(conn)
    await db.commit()

    return {
        "status": "pending_user_authorization",
        "link": link,
        "requisition_id": requisition_id,
        "agreement_id": agreement_id,
        "access_valid_for_days": access_days,
        "message": (
            "Send the user to `link`. After they approve at their bank, "
            "ClearFlow receives the callback and the connection becomes active."
        ),
    }


@router.get("/callback")
async def open_banking_callback(
    request: Request,
    ref: str | None = None,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Bank redirects here after consent.
    GoCardless/Nordigen typically appends ?ref=<requisition_id> (our reference may differ);
    we accept `ref` as requisition id when possible, else look up by reference string.
    """
    # Some flows pass the requisition id as `ref`; others echo our reference.
    raw_ref = ref or request.query_params.get("ref") or ""
    client = get_open_banking_client()

    requisition: dict[str, Any] | None = None
    requisition_id = raw_ref

    # If ref looks like our reference (cf:uuid:inst), find pending connection
    if raw_ref.startswith("cf:"):
        parts = raw_ref.split(":")
        if len(parts) >= 3:
            try:
                tenant_id = uuid.UUID(parts[1])
            except ValueError:
                tenant_id = None
            if tenant_id:
                result = await db.execute(
                    select(ProviderConnection).where(
                        ProviderConnection.tenant_id == tenant_id,
                        ProviderConnection.provider_name == PROVIDER_NAME,
                        ProviderConnection.status.in_(["pending", "connected"]),
                    )
                )
                conn = result.scalars().first()
                if conn and (conn.extra_data or {}).get("requisition_id"):
                    requisition_id = conn.extra_data["requisition_id"]

    if not requisition_id:
        raise HTTPException(status_code=400, detail="Missing ref / requisition id")

    try:
        requisition = await client.get_requisition(requisition_id)
    except OpenBankingError as exc:
        raise _http_error(exc) from exc

    accounts = requisition.get("accounts") or []
    status_ob = (requisition.get("status") or "").upper()
    reference = requisition.get("reference") or ""
    institution_id = requisition.get("institution_id") or ""

    tenant_id: uuid.UUID | None = None
    if reference.startswith("cf:"):
        try:
            tenant_id = uuid.UUID(reference.split(":")[1])
        except (IndexError, ValueError):
            tenant_id = None

    redirect_after = f"{FRONTEND_URL}/setup"
    if tenant_id:
        result = await db.execute(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant_id,
                ProviderConnection.provider_name == PROVIDER_NAME,
            )
        )
        # Prefer matching institution
        conns = list(result.scalars().all())
        conn = next(
            (c for c in conns if c.account_id == institution_id or (c.extra_data or {}).get("requisition_id") == requisition_id),
            conns[0] if conns else None,
        )
        if conn:
            redirect_after = (conn.extra_data or {}).get("redirect_after") or redirect_after
            conn.status = "connected" if accounts else "pending"
            conn.account_id = institution_id or conn.account_id
            extra = dict(conn.extra_data or {})
            extra.update(
                {
                    "requisition_id": requisition_id,
                    "requisition_status": status_ob,
                    "account_ids": accounts,
                    "connected_at": datetime.utcnow().isoformat(),
                    "access_expires_approx": (
                        datetime.utcnow() + timedelta(days=int(extra.get("access_valid_for_days") or 90))
                    ).isoformat(),
                    "status_detail": "connected" if accounts else f"requisition_{status_ob.lower()}",
                }
            )
            conn.extra_data = extra
            conn.last_sync_at = None
            await db.commit()

            # Create / update bank_accounts placeholders
            for account_id in accounts:
                details = {}
                try:
                    details = await client.get_account_details(account_id)
                except OpenBankingError:
                    details = {}
                acct = (details.get("account") or {}) if isinstance(details, dict) else {}
                iban = acct.get("iban")
                name = acct.get("name") or acct.get("ownerName") or "Open Banking account"
                existing_ba = await db.execute(
                    select(BankAccount).where(
                        BankAccount.tenant_id == tenant_id,
                        BankAccount.account_number == account_id,
                    )
                )
                ba = existing_ba.scalar_one_or_none()
                if ba:
                    ba.iban = iban or ba.iban
                    ba.name = name
                    ba.bank_name = institution_id
                    ba.is_active = 1
                else:
                    db.add(
                        BankAccount(
                            tenant_id=tenant_id,
                            name=name,
                            bank_name=institution_id,
                            account_number=account_id,
                            iban=iban,
                            currency=acct.get("currency") or "EUR",
                            is_active=1,
                        )
                    )
            await db.commit()

    qs = urlencode(
        {
            "open_banking": "ok" if accounts else "pending",
            "accounts": str(len(accounts)),
            "institution": institution_id,
        }
    )
    return RedirectResponse(url=f"{redirect_after}?{qs}", status_code=302)


@router.get("/connections/{tenant_id}")
async def list_connections(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_shared_db)):
    result = await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider_name == PROVIDER_NAME,
        )
    )
    conns = result.scalars().all()
    return {
        "tenant_id": str(tenant_id),
        "connections": [
            {
                "institution_id": c.account_id,
                "status": c.status,
                "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
                "extra": c.extra_data or {},
            }
            for c in conns
        ],
    }


@router.post("/sync/{tenant_id}")
async def sync_transactions(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    days: int = Query(default=90, ge=1, le=365),
):
    """Pull transactions for all connected Open Banking accounts of the tenant."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Open Banking not configured")

    result = await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider_name == PROVIDER_NAME,
            ProviderConnection.status == "connected",
        )
    )
    conns = list(result.scalars().all())
    if not conns:
        raise HTTPException(status_code=404, detail="No connected Open Banking link for this tenant")

    client = get_open_banking_client()
    date_from = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    synced = 0
    accounts_seen = 0

    for conn in conns:
        account_ids = (conn.extra_data or {}).get("account_ids") or []
        institution_id = conn.account_id
        for account_id in account_ids:
            accounts_seen += 1
            try:
                payload = await client.get_account_transactions(
                    account_id, date_from=date_from
                )
            except OpenBankingError as exc:
                logger.warning("open_banking_sync_failed account=%s err=%s", account_id, exc)
                continue

            booked = ((payload or {}).get("transactions") or {}).get("booked") or []
            pending = ((payload or {}).get("transactions") or {}).get("pending") or []
            for tx in list(booked) + list(pending):
                reference = (
                    tx.get("transactionId")
                    or tx.get("internalTransactionId")
                    or tx.get("entryReference")
                )
                if not reference:
                    # fallback stable-ish key
                    reference = f"{account_id}:{tx.get('bookingDate')}:{tx.get('transactionAmount', {}).get('amount')}:{tx.get('remittanceInformationUnstructured', '')[:40]}"

                existing = await db.execute(
                    select(BankTransaction).where(
                        BankTransaction.tenant_id == tenant_id,
                        BankTransaction.reference == str(reference),
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                amount_obj = tx.get("transactionAmount") or {}
                try:
                    amount = float(amount_obj.get("amount") or 0)
                except (TypeError, ValueError):
                    amount = 0.0

                date_str = tx.get("bookingDate") or tx.get("valueDate")
                try:
                    tx_date = (
                        datetime.fromisoformat(date_str)
                        if date_str
                        else datetime.utcnow()
                    )
                except ValueError:
                    tx_date = datetime.utcnow()

                concept = (
                    tx.get("remittanceInformationUnstructured")
                    or tx.get("additionalInformation")
                    or tx.get("creditorName")
                    or tx.get("debtorName")
                    or "Open Banking movement"
                )

                db.add(
                    BankTransaction(
                        tenant_id=tenant_id,
                        bank_name=institution_id,
                        transaction_date=tx_date,
                        concept=str(concept)[:2000],
                        amount=amount,
                        reference=str(reference)[:255],
                        matched=0,
                        filename="open_banking",
                        raw_data=json.dumps(tx),
                    )
                )
                synced += 1

        conn.last_sync_at = datetime.utcnow()
        extra = dict(conn.extra_data or {})
        extra["last_sync_count"] = synced
        conn.extra_data = extra

    await db.commit()
    return {
        "status": "synced",
        "tenant_id": str(tenant_id),
        "accounts": accounts_seen,
        "synced_count": synced,
        "date_from": date_from,
    }
