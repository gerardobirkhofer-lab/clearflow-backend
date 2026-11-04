from fastapi import APIRouter, HTTPException, Request, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import os
import stripe

# ClearFlow *billing* (our SaaS subscriptions) — not Stripe Connect for merchants.
router = APIRouter(prefix="/billing", tags=["billing"])

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
# Optional defaults from env (Stripe Dashboard → Products → Price IDs)
STRIPE_PRICE_STARTER = os.getenv("STRIPE_PRICE_STARTER", "")
STRIPE_PRICE_PRO = os.getenv("STRIPE_PRICE_PRO", "")
STRIPE_PRICE_ENTERPRISE = os.getenv("STRIPE_PRICE_ENTERPRISE", "")
TRIAL_DAYS = int(os.getenv("STRIPE_TRIAL_DAYS", "14"))

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class CreateCheckoutSessionRequest(BaseModel):
    price_id: Optional[str] = None
    plan: Optional[str] = None  # starter | pro | enterprise → resolves env price id
    mode: str = Field(default="subscription")
    customer_email: Optional[str] = None
    customer_id: Optional[str] = None
    line_items: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict)
    trial_days: Optional[int] = None
    tenant_id: Optional[str] = None


class CreateCustomerRequest(BaseModel):
    email: str
    name: Optional[str] = None
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict)


@router.get("/config")
def get_stripe_config():
    if not STRIPE_PUBLISHABLE_KEY:
        raise HTTPException(status_code=500, detail="Stripe publishable key not configured")
    return {"publishableKey": STRIPE_PUBLISHABLE_KEY}


def _resolve_price_id(body: CreateCheckoutSessionRequest) -> str | None:
    if body.price_id:
        return body.price_id
    plan = (body.plan or "").strip().lower()
    mapping = {
        "starter": STRIPE_PRICE_STARTER,
        "pro": STRIPE_PRICE_PRO,
        "enterprise": STRIPE_PRICE_ENTERPRISE,
    }
    return mapping.get(plan) or None


@router.get("/plans")
def list_billing_plans():
    """Public: which plans are wired (price ids present) + trial length."""
    return {
        "trial_days": TRIAL_DAYS,
        "currency_note": "Set amounts in Stripe Dashboard; ClearFlow only stores Price IDs.",
        "plans": {
            "starter": {"price_id_configured": bool(STRIPE_PRICE_STARTER), "label": "Starter"},
            "pro": {"price_id_configured": bool(STRIPE_PRICE_PRO), "label": "Pro"},
            "enterprise": {
                "price_id_configured": bool(STRIPE_PRICE_ENTERPRISE),
                "label": "Enterprise",
            },
        },
        "checkout": "/api/v1/billing/checkout-sessions",
    }


@router.post("/checkout-sessions", status_code=status.HTTP_201_CREATED)
def create_checkout_session(body: CreateCheckoutSessionRequest):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Billing not configured (STRIPE_SECRET_KEY missing)",
        )
    try:
        price_id = _resolve_price_id(body)
        meta = dict(body.metadata or {})
        if body.tenant_id:
            meta["tenant_id"] = body.tenant_id
        if body.plan:
            meta["plan"] = body.plan

        params: Dict[str, Any] = {
            "mode": body.mode,
            "success_url": f"{FRONTEND_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{FRONTEND_URL}/payment-cancel",
            "metadata": meta,
            "allow_promotion_codes": True,
        }
        if body.customer_id:
            params["customer"] = body.customer_id
        elif body.customer_email:
            params["customer_email"] = body.customer_email
        if body.line_items:
            params["line_items"] = body.line_items
        elif price_id:
            params["line_items"] = [{"price": price_id, "quantity": 1}]
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide price_id or plan=starter|pro|enterprise (and set STRIPE_PRICE_* env)",
            )

        # Self-serve trial on subscriptions
        if body.mode == "subscription":
            days = body.trial_days if body.trial_days is not None else TRIAL_DAYS
            if days and days > 0:
                params["subscription_data"] = {
                    "trial_period_days": int(days),
                    "metadata": meta,
                }

        session = stripe.checkout.Session.create(**params)
        return {
            "id": session.id,
            "url": session.url,
            "status": session.status,
            "mode": session.mode,
            "trial_days": (body.trial_days if body.trial_days is not None else TRIAL_DAYS)
            if body.mode == "subscription"
            else 0,
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/checkout-sessions/{session_id}")
def get_checkout_session(session_id: str):
    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["line_items", "customer"])
        return dict(session)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/customers", status_code=status.HTTP_201_CREATED)
def create_customer(body: CreateCustomerRequest):
    try:
        customer = stripe.Customer.create(email=body.email, name=body.name, metadata=body.metadata or {})
        return {"id": customer.id, "email": customer.email, "name": customer.name}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    try:
        return dict(stripe.Customer.retrieve(customer_id))
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None)):
    payload = await request.body()
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature or "", STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    print(f"[Stripe Webhook] {event['type']}")
    return JSONResponse(content={"status": "success"})