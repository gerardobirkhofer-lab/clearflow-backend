"""
Open Banking endpoints (multi-provider tube).

Active vendor: OPEN_BANKING_PROVIDER=gocardless|truelayer|tink

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
    available_providers,
    get_active_provider_name,
    get_open_banking_client,
    is_configured,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/open-banking", tags=["open-banking"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
API_PUBLIC_URL = os.getenv(
    "API_PUBLIC_URL",
    "https://clearflow-api-production.up.railway.app",
).rstrip("/")

# Legacy DB rows used this name for GoCardless connections
_LEGACY_PROVIDER_NAMES = {"gocardless_bank", "gocardless_bank_account_data", "nordigen"}


def _provider_name() -> str:
    return get_open_banking_client().name


def _provider_names_for_query() -> list[str]:
    active = _provider_name()
    names = {active}
    if active == "gocardless":
        names |= _LEGACY_PROVIDER_NAMES
    return list(names)


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
    client = get_open_banking_client()
    return {
        "ok": True,
        "provider": client.name,
        "provider_display": client.display_name,
        "configured": client.is_configured(),
        "providers": available_providers(),
        "access_days_default": int(os.getenv("OPEN_BANKING_ACCESS_DAYS", "90")),
        "history_days_default": int(os.getenv("OPEN_BANKING_HISTORY_DAYS", "90")),
        "callback_url": f"{API_PUBLIC_URL}/api/v1/open-banking/callback",
        "notes": client.config_notes(),
        "env": {
            "OPEN_BANKING_PROVIDER": get_active_provider_name(),
        },
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
    return {
        "provider": client.name,
        "country": country.upper(),
        "count": len(institutions),
        "institutions": [i.as_dict() for i in institutions],
    }


@router.post("/link")
async def create_bank_link(
    body: CreateLinkRequest,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Start Open Banking consent for a tenant + bank.
    Returns { link, session_id, agreement_id?, access_valid_for_days }.
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
    provider_name = client.name
    reference = f"cf:{body.tenant_id}:{body.institution_id}"
    callback = f"{API_PUBLIC_URL}/api/v1/open-banking/callback"

    try:
        session = await client.create_link(
            institution_id=body.institution_id,
            redirect_url=callback,
            reference=reference,
            user_language=(body.country or "ES").upper(),
        )
    except OpenBankingError as exc:
        raise _http_error(exc) from exc

    # Upsert pending connection
    existing = await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == body.tenant_id,
            ProviderConnection.provider_name.in_(_provider_names_for_query()),
            ProviderConnection.account_id == body.institution_id,
        )
    )
    conn = existing.scalar_one_or_none()
    extra = {
        "session_id": session.session_id,
        "requisition_id": session.session_id,  # backward-compatible key
        "agreement_id": session.agreement_id,
        "institution_id": body.institution_id,
        "access_valid_for_days": session.access_valid_for_days,
        "redirect_after": body.redirect_after or f"{FRONTEND_URL}/setup",
        "status_detail": "pending_user_authorization",
        "provider": provider_name,
    }
    if conn:
        conn.provider_name = provider_name
        conn.status = "pending"
        conn.extra_data = {**(conn.extra_data or {}), **extra}
    else:
        conn = ProviderConnection(
            tenant_id=body.tenant_id,
            provider_name=provider_name,
            account_id=body.institution_id,
            status="pending",
            extra_data=extra,
        )
        db.add(conn)
    await db.commit()

    return {
        "status": "pending_user_authorization",
        "provider": provider_name,
        "link": session.link,
        "session_id": session.session_id,
        "requisition_id": session.session_id,
        "agreement_id": session.agreement_id,
        "access_valid_for_days": session.access_valid_for_days,
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
    - GoCardless: ?ref=<session_id>
    - Tink: ?code=...&state=cf:tenant:institution
    """
    client = get_open_banking_client()
    provider_name = client.name
    callback_url = f"{API_PUBLIC_URL}/api/v1/open-banking/callback"

    oauth_error = request.query_params.get("error")
    if oauth_error:
        qs = urlencode(
            {
                "open_banking": "error",
                "error": oauth_error,
                "provider": provider_name,
            }
        )
        return RedirectResponse(url=f"{FRONTEND_URL}/setup?{qs}", status_code=302)

    code = request.query_params.get("code")
    state = request.query_params.get("state") or ""
    raw_ref = ref or request.query_params.get("ref") or ""
    user_access_token: str | None = None

    try:
        if code:
            link_status = await client.complete_authorization(
                code=code,
                state=state or raw_ref,
                redirect_uri=callback_url,
            )
            user_access_token = (link_status.raw or {}).get("access_token")
            session_id = link_status.session_id
        else:
            session_id = raw_ref
            if raw_ref.startswith("cf:"):
                parts = raw_ref.split(":")
                if len(parts) >= 3:
                    try:
                        maybe_tenant = uuid.UUID(parts[1])
                    except ValueError:
                        maybe_tenant = None
                    if maybe_tenant:
                        result = await db.execute(
                            select(ProviderConnection).where(
                                ProviderConnection.tenant_id == maybe_tenant,
                                ProviderConnection.provider_name.in_(_provider_names_for_query()),
                                ProviderConnection.status.in_(["pending", "connected"]),
                            )
                        )
                        conn = result.scalars().first()
                        if conn:
                            extra = conn.extra_data or {}
                            session_id = (
                                extra.get("session_id")
                                or extra.get("requisition_id")
                                or session_id
                            )
            if not session_id:
                raise HTTPException(status_code=400, detail="Missing ref / session id / code")
            link_status = await client.get_link_status(session_id)
    except OpenBankingError as exc:
        raise _http_error(exc) from exc

    accounts = link_status.accounts
    status_ob = link_status.status
    reference = link_status.reference or state or raw_ref
    institution_id = link_status.institution_id
    session_id = link_status.session_id

    tenant_id: uuid.UUID | None = None
    if reference.startswith("cf:"):
        try:
            tenant_id = uuid.UUID(reference.split(":")[1])
        except (IndexError, ValueError):
            tenant_id = None
        if not institution_id and len(reference.split(":")) >= 3:
            institution_id = reference.split(":")[2]

    redirect_after = f"{FRONTEND_URL}/setup"
    if tenant_id:
        result = await db.execute(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant_id,
                ProviderConnection.provider_name.in_(_provider_names_for_query()),
            )
        )
        conns = list(result.scalars().all())
        conn = next(
            (
                c
                for c in conns
                if c.account_id == institution_id
                or (c.extra_data or {}).get("session_id") == session_id
                or (c.extra_data or {}).get("requisition_id") == session_id
                or (c.extra_data or {}).get("session_id") == reference
            ),
            conns[0] if conns else None,
        )
        if conn:
            redirect_after = (conn.extra_data or {}).get("redirect_after") or redirect_after
            conn.provider_name = provider_name
            conn.status = "connected" if accounts else "pending"
            conn.account_id = institution_id or conn.account_id
            extra = dict(conn.extra_data or {})
            extra.update(
                {
                    "session_id": session_id,
                    "requisition_id": session_id,
                    "requisition_status": status_ob,
                    "account_ids": accounts,
                    "connected_at": datetime.utcnow().isoformat(),
                    "access_expires_approx": (
                        datetime.utcnow()
                        + timedelta(days=int(extra.get("access_valid_for_days") or 90))
                    ).isoformat(),
                    "status_detail": "connected" if accounts else f"requisition_{status_ob.lower()}",
                    "provider": provider_name,
                }
            )
            if user_access_token:
                extra["user_access_token"] = user_access_token
                expires_in = (link_status.raw or {}).get("expires_in")
                if expires_in:
                    extra["user_access_token_expires_at"] = (
                        datetime.utcnow() + timedelta(seconds=int(expires_in))
                    ).isoformat()
            conn.extra_data = extra
            conn.last_sync_at = None
            await db.commit()

            for account_id in accounts:
                details: dict[str, Any] = {}
                try:
                    details = await client.get_account_details(
                        account_id,
                        user_access_token=user_access_token,
                    )
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
            "provider": provider_name,
        }
    )
    return RedirectResponse(url=f"{redirect_after}?{qs}", status_code=302)


@router.get("/transactions/{tenant_id}")
async def list_open_banking_transactions(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_shared_db),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Bank movements ingested via Open Banking sync (filename=open_banking)."""
    result = await db.execute(
        select(BankTransaction)
        .where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.filename == "open_banking",
        )
        .order_by(BankTransaction.transaction_date.desc())
        .limit(limit)
    )
    txs = result.scalars().all()
    return {
        "tenant_id": str(tenant_id),
        "total_count": len(txs),
        "transactions": [
            {
                "id": t.id,
                "concept": t.concept,
                "amount": t.amount,
                "date": t.transaction_date.isoformat() if t.transaction_date else None,
                "reference": t.reference,
                "bank_name": t.bank_name,
                "matched": bool(t.matched),
                "source": "open_banking",
                "type": "bank",
            }
            for t in txs
        ],
    }


@router.get("/connections/{tenant_id}")
async def list_connections(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_shared_db)):
    result = await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider_name.in_(_provider_names_for_query()),
        )
    )
    conns = result.scalars().all()
    def _public_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(extra or {})
        # Never expose live user tokens in API responses
        if data.get("user_access_token"):
            data["user_access_token"] = "[redacted]"
        return data

    return {
        "tenant_id": str(tenant_id),
        "provider": _provider_name(),
        "connections": [
            {
                "institution_id": c.account_id,
                "provider": c.provider_name,
                "status": c.status,
                "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
                "extra": _public_extra(c.extra_data if isinstance(c.extra_data, dict) else {}),
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
            ProviderConnection.provider_name.in_(_provider_names_for_query()),
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
    account_errors: list[str] = []

    for conn in conns:
        account_ids = (conn.extra_data or {}).get("account_ids") or []
        institution_id = conn.account_id
        user_access_token = (conn.extra_data or {}).get("user_access_token")
        for account_id in account_ids:
            accounts_seen += 1
            try:
                txs = await client.get_account_transactions(
                    account_id,
                    date_from=date_from,
                    user_access_token=user_access_token,
                )
            except OpenBankingError as exc:
                logger.warning("open_banking_sync_failed account=%s err=%s", account_id, exc)
                account_errors.append(str(exc))
                continue

            for tx in txs:
                existing = await db.execute(
                    select(BankTransaction).where(
                        BankTransaction.tenant_id == tenant_id,
                        BankTransaction.reference == str(tx.reference)[:255],
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                date_str = tx.booking_date
                try:
                    tx_date = datetime.fromisoformat(date_str) if date_str else datetime.utcnow()
                except ValueError:
                    tx_date = datetime.utcnow()

                db.add(
                    BankTransaction(
                        tenant_id=tenant_id,
                        bank_name=institution_id,
                        transaction_date=tx_date,
                        concept=str(tx.concept)[:2000],
                        amount=tx.amount,
                        reference=str(tx.reference)[:255],
                        matched=0,
                        filename="open_banking",
                        raw_data=json.dumps(tx.raw),
                    )
                )
                synced += 1

        conn.last_sync_at = datetime.utcnow()
        extra = dict(conn.extra_data or {})
        extra["last_sync_count"] = synced
        extra["provider"] = client.name
        conn.extra_data = extra

    await db.commit()

    if accounts_seen > 0 and synced == 0 and len(account_errors) >= accounts_seen:
        joined = "; ".join(account_errors[:3])
        raise HTTPException(
            status_code=401 if any("token" in e.lower() or "unauthor" in e.lower() or "401" in e for e in account_errors) else 502,
            detail=(
                "No se pudieron sincronizar las cuentas. "
                "La autorización del banco probablemente caducó: volvé a conectar el banco. "
                f"Detalle: {joined}"
            ),
        )

    return {
        "status": "synced",
        "provider": client.name,
        "tenant_id": str(tenant_id),
        "accounts": accounts_seen,
        "synced_count": synced,
        "date_from": date_from,
        "account_errors": account_errors[:5] if account_errors else [],
    }
