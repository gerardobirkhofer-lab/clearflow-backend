import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

import stripe
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, get_shared_db
from app.models.provider_connection import ProviderConnection
from app.models.provider_transaction import ProviderTransaction
from app.models_orm import Tenant

logger = logging.getLogger(__name__)
router = APIRouter()

STRIPE_CLIENT_ID = os.getenv("STRIPE_CLIENT_ID", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
DEFAULT_TENANT_ID = os.getenv(
    "DEFAULT_TENANT_ID",
    "22222222-2222-2222-2222-222222222222",
)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION A: Direct API Key (recommended for immediate use)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/connect-direct")
async def connect_stripe_direct(
    data: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Connect a Stripe account using a direct API key (sk_live_... or sk_test_...).
    Validates the key by fetching the account info from Stripe.
    """
    tenant_id_str = data.get("tenant_id")
    api_key = data.get("api_key", "").strip()

    if not tenant_id_str or not api_key:
        raise HTTPException(status_code=422, detail="tenant_id and api_key are required")

    try:
        tenant_id = uuid.UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id")

    # Verify tenant exists
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Validate the API key with Stripe (single clean attempt)
    try:
        original_key = stripe.api_key
        stripe.api_key = api_key
        account = stripe.Account.retrieve()
        account_id = account.id
        account_email = getattr(account, 'email', '') or ''
        stripe.api_key = original_key
    except stripe.error.AuthenticationError:
        stripe.api_key = original_key
        raise HTTPException(status_code=401, detail="Invalid Stripe API key. Please check and try again.")
    except Exception as e:
        stripe.api_key = original_key
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}")

    # Save or update connection
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    existing = result.scalar_one_or_none()

    if existing:
        existing.account_id = account_id
        existing.access_token = api_key
        existing.status = "connected"
        existing.extra_data = {
            "mode": "direct_api_key",
            "account_email": account_email,
        }
    else:
        conn = ProviderConnection(
            tenant_id=tenant_id,
            provider_name="stripe",
            account_id=account_id,
            access_token=api_key,
            status="connected",
            extra_data={"mode": "direct_api_key", "account_email": account_email}
        )
        db.add(conn)

    await db.commit()

    return {
        "status": "connected",
        "account_id": account_id,
        "mode": "direct_api_key",
        "message": "Stripe connected successfully. Click Sync to fetch transactions."
    }


@router.get("/status/{tenant_id}")
async def get_stripe_status(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    conn = result.scalar_one_or_none()
    if not conn:
        return {"connected": False, "status": "not_connected", "mode": None}

    mode = (conn.extra_data or {}).get("mode", "oauth")
    return {
        "connected": conn.status == "connected",
        "status": conn.status,
        "account_id": conn.account_id,
        "mode": mode,
        "last_sync_at": conn.last_sync_at.isoformat() if conn.last_sync_at else None,
    }


@router.post("/sync/{tenant_id}")
async def manual_sync(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """
    Manually trigger a full sync of Stripe balance transactions for a tenant.
    Supports both OAuth (Connect) and direct API key modes.
    """
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    conn = result.scalar_one_or_none()
    if not conn or not conn.access_token:
        raise HTTPException(status_code=400, detail="Stripe not connected. Please connect your Stripe account first.")

    mode = (conn.extra_data or {}).get("mode", "oauth")
    api_key = conn.access_token

    # Use the tenant's own API key for direct mode, or our platform key + stripe_account for OAuth
    if mode == "direct_api_key":
        stripe.api_key = api_key
        stripe_account = None
    else:
        stripe.api_key = STRIPE_SECRET_KEY
        stripe_account = conn.account_id

    synced_count = 0
    try:
        # Fetch balance transactions from last 90 days
        start_timestamp = int((datetime.utcnow() - timedelta(days=90)).timestamp())

        params = {
            "limit": 100,
            "created": {"gte": start_timestamp},
        }
        if stripe_account:
            params["stripe_account"] = stripe_account

        balance_transactions = stripe.balance.Transaction.list(**params)

        for bt in balance_transactions.auto_paging_iter():
            # Avoid duplicates by checking source ID
            source_id = bt.source or bt.id
            existing = await db.execute(select(ProviderTransaction).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.provider_name == "stripe",
                ProviderTransaction.reference == source_id
            ))
            if existing.scalar_one_or_none():
                continue

            # Map Stripe types to ClearFlow concepts
            type_map = {
                "charge": f"Stripe Charge {source_id[-12:]}",
                "refund": f"Stripe Refund {source_id[-12:]}",
                "payout": f"Stripe Payout {source_id[-12:]}",
                "fee": f"Stripe Fee {source_id[-12:]}",
                "adjustment": f"Stripe Adjustment {source_id[-12:]}",
                "transfer": f"Stripe Transfer {source_id[-12:]}",
                "payment": f"Stripe Payment {source_id[-12:]}",
            }
            concept = type_map.get(bt.type, f"Stripe {bt.type.title()} {source_id[-12:]}")

            # Amount is in cents, convert to euros
            amount_eur = float(bt.amount) / 100.0
            fee_eur = float(bt.fee) / 100.0
            net_eur = float(bt.net) / 100.0
            tx_date = datetime.fromtimestamp(bt.created)

            from app.services.contractual_audit import apply_contract_recalc, load_provider_terms

            terms = await load_provider_terms(db, tenant_id, "stripe")
            audit = apply_contract_recalc(
                amount=amount_eur,
                gross_amount=amount_eur,
                fee_amount=fee_eur,
                net_amount=net_eur,
                terms=terms,
                sale_date=tx_date,
            )

            tx = ProviderTransaction(
                tenant_id=tenant_id,
                provider_name="stripe",
                amount=amount_eur,
                transaction_date=tx_date,
                concept=concept,
                reference=source_id,
                matched=0,
                match_status="pending",
                gross_amount=amount_eur,
                fee_amount=fee_eur,
                net_amount=net_eur,
                gateway_reported_net=audit["gateway_reported_net"],
                contract_gross_basis=audit["contract_gross_basis"],
                contract_expected_fee=audit["contract_expected_fee"],
                contract_expected_net=audit["contract_expected_net"],
                contract_audit_status=audit["contract_audit_status"],
                contract_audit_json=audit["contract_audit_json"],
                raw_data=json.dumps(
                    {
                        "stripe_type": bt.type,
                        "currency": (bt.currency or "eur").upper(),
                        "fee": fee_eur,
                        "net": net_eur,
                        "status": bt.status,
                        "description": bt.description or "",
                    }
                ),
            )
            db.add(tx)
            synced_count += 1

            # Commit every 500 to avoid memory issues
            if synced_count % 500 == 0:
                await db.commit()

        conn.last_sync_at = datetime.utcnow()
        await db.commit()

        return {
            "status": "synced",
            "synced_count": synced_count,
            "last_sync_at": conn.last_sync_at.isoformat()
        }

    except stripe.error.AuthenticationError:
        raise HTTPException(status_code=401, detail="Stripe API key is invalid or revoked. Please reconnect.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/disconnect/{tenant_id}")
async def disconnect_stripe(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Remove Stripe connection for a tenant."""
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    conn = result.scalar_one_or_none()
    if conn:
        conn.status = "disconnected"
        conn.access_token = None
        await db.commit()
    return {"status": "disconnected"}


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION B: OAuth Connect (kept for future use)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/connect-url")
async def get_stripe_connect_url(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not STRIPE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Stripe Connect not configured")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    url = f"https://connect.stripe.com/oauth/authorize?response_type=code&client_id={STRIPE_CLIENT_ID}&scope=read_only&state={tenant_id}"
    return {"url": url}


@router.get("/callback")
async def stripe_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    tenant_id = uuid.UUID(state)
    try:
        response = stripe.oauth.token(grant_type="authorization_code", code=code)
        stripe_user_id = response.get("stripe_user_id")
        access_token = response.get("access_token")

        result = await db.execute(select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider_name == "stripe"
        ))
        existing = result.scalar_one_or_none()

        if existing:
            existing.account_id = stripe_user_id
            existing.access_token = access_token
            existing.status = "connected"
            existing.extra_data = {"mode": "oauth", "scope": response.get("scope", "")}
        else:
            conn = ProviderConnection(
                tenant_id=tenant_id,
                provider_name="stripe",
                account_id=stripe_user_id,
                access_token=access_token,
                status="connected",
                extra_data={"mode": "oauth", "scope": response.get("scope", "")}
            )
            db.add(conn)
        await db.commit()
        return {"status": "success", "account_id": stripe_user_id}
    except stripe.error.OAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  Webhooks — public endpoint (NO JWT). Stripe cannot authenticate as a user.
#  Dashboard URL: https://<api-host>/api/v1/stripe/webhook
# ═══════════════════════════════════════════════════════════════════════════════

HANDLED_STRIPE_EVENTS = {
    "charge.succeeded",
    "charge.refunded",
    "charge.failed",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payout.paid",
    "payout.failed",
    "balance.available",
    "checkout.session.completed",
}


@router.get("/webhook")
async def stripe_webhook_info() -> dict[str, Any]:
    """Browser check before saving the endpoint in Stripe Dashboard."""
    return {
        "ok": True,
        "service": "clearflow",
        "endpoint": "/api/v1/stripe/webhook",
        "method": "POST",
        "signature_required": bool(STRIPE_WEBHOOK_SECRET),
        "provider": "stripe",
        "handled_events": sorted(HANDLED_STRIPE_EVENTS),
    }


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Receive Stripe events and persist charges/payouts as provider_transactions.

    Paste in Stripe Dashboard → Developers → Webhooks:
      https://clearflow-api-production.up.railway.app/api/v1/stripe/webhook
    Then set STRIPE_WEBHOOK_SECRET in Railway to the endpoint signing secret (whsec_...).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header or "", STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError:
            logger.warning("stripe_webhook_invalid_signature")
            raise HTTPException(status_code=401, detail="Invalid Stripe webhook signature")
    else:
        logger.warning(
            "stripe_webhook_secret_missing — accepting without verification; "
            "set STRIPE_WEBHOOK_SECRET after creating the endpoint in Stripe"
        )
        try:
            event = json.loads(payload.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Stripe SDK returns StripeObject; normalize to dict
    if hasattr(event, "to_dict"):
        event = event.to_dict()

    event_type = event.get("type") or ""
    event_id = event.get("id")
    account_id = event.get("account")  # present for Connect events
    obj = (event.get("data") or {}).get("object") or {}

    logger.info(
        "stripe_webhook event_id=%s type=%s account=%s",
        event_id,
        event_type,
        account_id,
    )

    if event_type in HANDLED_STRIPE_EVENTS:
        background_tasks.add_task(
            _persist_stripe_event,
            event_type=event_type,
            event_id=event_id or "",
            account_id=account_id,
            obj=obj,
        )

    return {"status": "received", "type": event_type}


async def _resolve_tenant_id(db: AsyncSession, account_id: str | None) -> uuid.UUID | None:
    """Map Stripe account → ClearFlow tenant via provider_connections; else default tenant."""
    if account_id:
        result = await db.execute(
            select(ProviderConnection).where(
                ProviderConnection.account_id == account_id,
                ProviderConnection.provider_name == "stripe",
            )
        )
        conn = result.scalar_one_or_none()
        if conn:
            return conn.tenant_id

    # Platform / direct-key mode: use default demo tenant if configured
    try:
        return uuid.UUID(DEFAULT_TENANT_ID)
    except ValueError:
        return None


def _amount_from_stripe_object(obj: dict[str, Any]) -> float:
    """Stripe amounts are in the smallest currency unit (cents)."""
    amount = obj.get("amount")
    if amount is None:
        amount = obj.get("amount_total")
    if amount is None:
        amount = obj.get("amount_received")
    try:
        return float(amount or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _concept_for_event(event_type: str, obj: dict[str, Any]) -> str:
    ref = (obj.get("id") or "")[-12:]
    mapping = {
        "charge.succeeded": f"Stripe Charge {ref}",
        "charge.refunded": f"Stripe Refund {ref}",
        "charge.failed": f"Stripe Charge Failed {ref}",
        "payment_intent.succeeded": f"Stripe PaymentIntent {ref}",
        "payment_intent.payment_failed": f"Stripe PaymentIntent Failed {ref}",
        "payout.paid": f"Stripe Payout {ref}",
        "payout.failed": f"Stripe Payout Failed {ref}",
        "checkout.session.completed": f"Stripe Checkout {ref}",
        "balance.available": "Stripe Balance Available",
    }
    return mapping.get(event_type, f"Stripe {event_type} {ref}")


async def _persist_stripe_event(
    event_type: str,
    event_id: str,
    account_id: str | None,
    obj: dict[str, Any],
) -> None:
    """Upsert a provider_transaction from a Stripe event (idempotent by reference)."""
    from app.core.database import SharedSessionLocal

    # balance.available has no single tx id worth storing as a line item
    if event_type == "balance.available":
        logger.info("stripe_balance_available account=%s", account_id)
        return

    reference = obj.get("id") or event_id
    if not reference:
        return

    async with SharedSessionLocal() as db:
        tenant_id = await _resolve_tenant_id(db, account_id)
        if tenant_id is None:
            logger.warning(
                "stripe_event_no_tenant event_id=%s account=%s", event_id, account_id
            )
            return

        existing = await db.execute(
            select(ProviderTransaction).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.provider_name == "stripe",
                ProviderTransaction.reference == reference,
            )
        )
        if existing.scalar_one_or_none():
            return

        from app.services.ingest_utils import fingerprint, classify_kind

        created_ts = obj.get("created")
        tx_date = (
            datetime.fromtimestamp(created_ts)
            if isinstance(created_ts, (int, float))
            else datetime.utcnow()
        )

        amount = _amount_from_stripe_object(obj)
        # Refunds / failed payouts: keep sign informative for reconciliation
        if event_type in {"charge.refunded", "payout.failed"} and amount > 0:
            amount = -amount

        concept = _concept_for_event(event_type, obj)
        settle_id = None
        if event_type.startswith("payout"):
            settle_id = str(reference)
        fee = None
        net = None
        gross = None
        if isinstance(obj.get("fee"), (int, float)):
            fee = float(obj["fee"]) / 100.0
        if isinstance(obj.get("amount"), (int, float)) and fee is not None:
            gross = abs(float(obj["amount"])) / 100.0
            net = gross - fee

        fp = fingerprint(
            tenant_id,
            channel="webhook_stripe",
            reference=str(reference),
            amount=amount,
            tx_date=tx_date,
            concept=concept,
            provider_name="stripe",
        )
        dup = await db.execute(
            select(ProviderTransaction.id).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.ingest_fingerprint == fp,
            )
        )
        if dup.scalar_one_or_none() is not None:
            return

        from app.services.contractual_audit import apply_contract_recalc, load_provider_terms

        terms = await load_provider_terms(db, tenant_id, "stripe")
        audit = apply_contract_recalc(
            amount=amount,
            gross_amount=gross,
            fee_amount=fee,
            net_amount=net,
            terms=terms,
            sale_date=tx_date,
        )

        tx = ProviderTransaction(
            tenant_id=tenant_id,
            provider_name="stripe",
            amount=amount,
            transaction_date=tx_date,
            concept=concept,
            reference=reference,
            matched=0,
            match_status="pending",
            raw_data=json.dumps(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "account_id": account_id,
                    "currency": (obj.get("currency") or "eur").upper(),
                    "status": obj.get("status"),
                    "description": obj.get("description") or obj.get("statement_descriptor"),
                }
            ),
            ingest_fingerprint=fp,
            tx_kind=classify_kind(
                channel="webhook_stripe",
                concept=f"{event_type} {concept}",
                filename=event_type,
            ),
            settlement_id=settle_id,
            gross_amount=gross,
            fee_amount=fee,
            net_amount=net,
            gateway_reported_net=audit["gateway_reported_net"],
            contract_gross_basis=audit["contract_gross_basis"],
            contract_expected_fee=audit["contract_expected_fee"],
            contract_expected_net=audit["contract_expected_net"],
            contract_audit_status=audit["contract_audit_status"],
            contract_audit_json=audit["contract_audit_json"],
        )
        db.add(tx)

        # Touch connection last_sync if we know the account
        if account_id:
            result = await db.execute(
                select(ProviderConnection).where(
                    ProviderConnection.account_id == account_id,
                    ProviderConnection.provider_name == "stripe",
                )
            )
            conn = result.scalar_one_or_none()
            if conn:
                conn.last_sync_at = datetime.utcnow()

        await db.commit()
        logger.info(
            "stripe_tx_saved tenant=%s reference=%s type=%s amount=%s",
            tenant_id,
            reference,
            event_type,
            amount,
        )
