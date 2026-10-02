import uuid
import os
import stripe
import jwt
from datetime import datetime, timedelta, timezone
from app.core.secrets import jwt_secret
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.auth import CurrentUser, get_current_user
from app.core.database import SharedSessionLocal, get_db
from app.core.secrets import decrypt_secret, encrypt_secret
from app.core.tenant_access import bind_tenant
from app.models.provider_connection import ProviderConnection
from app.models.provider_transaction import ProviderTransaction
from app.models_orm import Tenant

router = APIRouter()

STRIPE_CLIENT_ID = os.getenv("STRIPE_CLIENT_ID", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION A: Direct API Key (recommended for immediate use)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/connect-direct")
async def connect_stripe_direct(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
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
    tenant_id = bind_tenant(current_user, tenant_id)

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
        existing.access_token = encrypt_secret(api_key)
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
            access_token=encrypt_secret(api_key),
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
async def get_stripe_status(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
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
async def manual_sync(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
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
    api_key = decrypt_secret(conn.access_token)

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

            tx = ProviderTransaction(
                tenant_id=tenant_id,
                provider_name="stripe",
                amount=amount_eur,
                currency=bt.currency.upper() if bt.currency else "EUR",
                transaction_date=datetime.fromtimestamp(bt.created),
                concept=concept,
                reference=source_id,
                matched=False,
                extra_data={
                    "stripe_type": bt.type,
                    "fee": fee_eur,
                    "net": net_eur,
                    "status": bt.status,
                    "description": bt.description or "",
                }
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
async def disconnect_stripe(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
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
async def get_stripe_connect_url(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = bind_tenant(current_user, tenant_id)
    if not STRIPE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Stripe Connect not configured")
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    state = jwt.encode(
        {
            "tenant_id": str(tenant_id),
            "purpose": "stripe_oauth",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        jwt_secret(),
        algorithm="HS256",
    )
    url = f"https://connect.stripe.com/oauth/authorize?response_type=code&client_id={STRIPE_CLIENT_ID}&scope=read_only&state={state}"
    return {"url": url}


@router.get("/callback")
async def stripe_callback(code: str, state: str):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    try:
        claims = jwt.decode(state, jwt_secret(), algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    if claims.get("purpose") != "stripe_oauth":
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    tenant_id = uuid.UUID(claims["tenant_id"])
    try:
        response = stripe.oauth.token(grant_type="authorization_code", code=code)
    except stripe.error.OAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    stripe_user_id = response.get("stripe_user_id")
    access_token = response.get("access_token")
    async with SharedSessionLocal() as db:
        result = await db.execute(select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider_name == "stripe"
        ))
        existing = result.scalar_one_or_none()
        if existing:
            existing.account_id = stripe_user_id
            existing.access_token = encrypt_secret(access_token or "")
            existing.status = "connected"
            existing.extra_data = {"mode": "oauth", "scope": response.get("scope", "")}
        else:
            db.add(ProviderConnection(
                tenant_id=tenant_id,
                provider_name="stripe",
                account_id=stripe_user_id,
                access_token=encrypt_secret(access_token or ""),
                status="connected",
                extra_data={"mode": "oauth", "scope": response.get("scope", "")}
            ))
        await db.commit()
    return {"status": "success", "account_id": stripe_user_id}


# ═══════════════════════════════════════════════════════════════════════════════
#  Webhooks (kept for future use)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/checkout-sessions", status_code=201)
async def create_checkout_session(
    data: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Start a subscription checkout for the caller's own tenant."""
    plan = (data.get("plan") or "").strip().lower()
    price_env = {"pro": "STRIPE_PRICE_PRO", "enterprise": "STRIPE_PRICE_ENTERPRISE"}.get(plan)
    if price_env is None:
        raise HTTPException(status_code=422, detail="plan must be pro or enterprise")
    price_id = os.getenv(price_env, "").strip()
    if not STRIPE_SECRET_KEY or not price_id:
        raise HTTPException(status_code=503, detail="Billing is not configured")
    stripe.api_key = STRIPE_SECRET_KEY
    frontend = os.getenv("FRONTEND_URL", "http://localhost:3000")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=current_user.email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{frontend}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{frontend}/payment-cancel",
        client_reference_id=str(current_user.tenant_id),
        metadata={"tenant_id": str(current_user.tenant_id), "plan": plan},
    )
    return {"id": session.id, "url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not STRIPE_WEBHOOK_SECRET or not sig_header:
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type")
    if event_type == "checkout.session.completed":
        await _apply_checkout(event.get("data", {}).get("object") or {})

    if event_type in ["payout.paid", "payout.created"]:
        account_id = event.get("account")
        payout_id = event.get("data", {}).get("object", {}).get("id")
        if account_id and payout_id:
            background_tasks.add_task(_sync_single_payout_oauth, account_id, payout_id)

    return {"status": "received"}


async def _apply_checkout(session_obj: dict) -> None:
    """Set the paid plan on the tenant named in the Checkout metadata."""
    from app.models_orm import Tenant, TenantTier

    metadata = session_obj.get("metadata") or {}
    plan = (metadata.get("plan") or "").lower()
    tenant_raw = metadata.get("tenant_id")
    if plan not in ("pro", "enterprise", "starter") or not tenant_raw:
        return
    try:
        tenant_id = uuid.UUID(tenant_raw)
    except ValueError:
        return
    async with SharedSessionLocal() as db:
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            return
        tenant.subscription_plan = plan
        tenant.tier = TenantTier(plan)
        await db.commit()


async def _sync_single_payout_oauth(account_id: str, payout_id: str):
    """Fetch a specific payout via OAuth and save to DB."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ProviderConnection).where(
            ProviderConnection.account_id == account_id,
            ProviderConnection.provider_name == "stripe"
        ))
        conn = result.scalar_one_or_none()
        if not conn:
            return

        try:
            stripe.api_key = STRIPE_SECRET_KEY
            payout = stripe.payout.retrieve(payout_id, stripe_account=account_id)

            existing = await db.execute(select(ProviderTransaction).where(
                ProviderTransaction.tenant_id == conn.tenant_id,
                ProviderTransaction.provider_name == "stripe",
                ProviderTransaction.reference == payout_id
            ))
            if existing.scalar_one_or_none():
                return

            tx = ProviderTransaction(
                tenant_id=conn.tenant_id,
                provider_name="stripe",
                amount=float(payout.amount) / 100,
                currency=payout.currency.upper(),
                transaction_date=datetime.fromtimestamp(payout.created),
                concept=f"Stripe Payout {payout_id[-12:]}",
                reference=payout_id,
                matched=False,
                extra_data={
                    "payout_id": payout_id,
                    "status": payout.status,
                    "method": payout.method,
                }
            )
            db.add(tx)
            conn.last_sync_at = datetime.utcnow()
            await db.commit()
        except Exception as e:
            print(f"Stripe webhook sync error: {e}")
