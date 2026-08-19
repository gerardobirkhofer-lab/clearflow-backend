import uuid
import os
import stripe
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.provider_connection import ProviderConnection
from app.models.provider_transaction import ProviderTransaction
from app.models_orm import Tenant

router = APIRouter()

STRIPE_CLIENT_ID = os.getenv("STRIPE_CLIENT_ID", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY


@router.get("/connect-url")
async def get_stripe_connect_url(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not STRIPE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Stripe not configured")
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
        else:
            conn = ProviderConnection(
                tenant_id=tenant_id,
                provider_name="stripe",
                account_id=stripe_user_id,
                access_token=access_token,
                status="connected",
                extra_data={"scope": response.get("scope", "")}
            )
            db.add(conn)
        await db.commit()
        return {"status": "success", "account_id": stripe_user_id}
    except stripe.error.OAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/status/{tenant_id}")
async def get_stripe_status(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    conn = result.scalar_one_or_none()
    if not conn:
        return {"connected": False, "status": "not_connected"}
    return {
        "connected": conn.status == "connected",
        "status": conn.status,
        "account_id": conn.account_id,
        "last_sync_at": conn.last_sync_at.isoformat() if conn.last_sync_at else None,
    }


@router.post("/webhook")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Receive Stripe events and process payouts automatically."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    if STRIPE_WEBHOOK_SECRET and sig_header:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        event = await request.json()
    
    event_type = event.get("type")
    
    if event_type in ["payout.paid", "payout.created"]:
        account_id = event.get("account")
        payout_id = event.get("data", {}).get("object", {}).get("id")
        if account_id and payout_id:
            background_tasks.add_task(sync_stripe_payout, account_id, payout_id)
    
    if event_type == "balance.available":
        account_id = event.get("account")
        if account_id:
            background_tasks.add_task(sync_recent_payouts, account_id)
    
    return {"status": "received"}


async def sync_stripe_payout(account_id: str, payout_id: str):
    """Fetch a specific payout and save to DB."""
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
                ProviderTransaction.concept == f"Stripe Payout {payout_id}"
            ))
            if existing.scalar_one_or_none():
                return
            
            tx = ProviderTransaction(
                tenant_id=conn.tenant_id,
                provider_name="stripe",
                amount=float(payout.amount) / 100,
                currency=payout.currency.upper(),
                transaction_date=payout.arrival_date,
                concept=f"Stripe Payout {payout_id}",
                matched=False,
                extra_data={
                    "payout_id": payout_id,
                    "status": payout.status,
                    "method": payout.method,
                    "bank_account": payout.destination,
                }
            )
            db.add(tx)
            conn.last_sync_at = datetime.utcnow()
            await db.commit()
        except Exception as e:
            print(f"Stripe sync error: {e}")


async def sync_recent_payouts(account_id: str):
    """Fetch last 30 days of payouts for an account."""
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
            payouts = stripe.payout.list(
                limit=100,
                stripe_account=account_id,
                created={"gte": int((datetime.utcnow() - timedelta(days=30)).timestamp())}
            )
            
            for payout in payouts.auto_paging_iter():
                existing = await db.execute(select(ProviderTransaction).where(
                    ProviderTransaction.tenant_id == conn.tenant_id,
                    ProviderTransaction.provider_name == "stripe",
                    ProviderTransaction.concept == f"Stripe Payout {payout.id}"
                ))
                if existing.scalar_one_or_none():
                    continue
                
                tx = ProviderTransaction(
                    tenant_id=conn.tenant_id,
                    provider_name="stripe",
                    amount=float(payout.amount) / 100,
                    currency=payout.currency.upper(),
                    transaction_date=payout.arrival_date,
                    concept=f"Stripe Payout {payout.id}",
                    matched=False,
                    extra_data={
                        "payout_id": payout.id,
                        "status": payout.status,
                        "method": payout.method,
                    }
                )
                db.add(tx)
            
            conn.last_sync_at = datetime.utcnow()
            await db.commit()
        except Exception as e:
            print(f"Stripe bulk sync error: {e}")


@router.post("/sync/{tenant_id}")
async def manual_sync(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Manually trigger a sync for a tenant's Stripe account."""
    result = await db.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id,
        ProviderConnection.provider_name == "stripe"
    ))
    conn = result.scalar_one_or_none()
    if not conn or not conn.access_token:
        raise HTTPException(status_code=400, detail="Stripe not connected")
    
    await sync_recent_payouts(conn.account_id)
    return {"status": "synced", "last_sync_at": datetime.utcnow().isoformat()}
