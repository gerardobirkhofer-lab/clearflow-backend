from fastapi import APIRouter, HTTPException, Request, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import os
import stripe

router = APIRouter(prefix="/stripe", tags=["stripe"])

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class CreateCheckoutSessionRequest(BaseModel):
    price_id: Optional[str] = None
    mode: str = Field(default="payment")
    customer_email: Optional[str] = None
    customer_id: Optional[str] = None
    line_items: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict)


class CreateCustomerRequest(BaseModel):
    email: str
    name: Optional[str] = None
    metadata: Optional[Dict[str, str]] = Field(default_factory=dict)


@router.get("/config")
def get_stripe_config():
    if not STRIPE_PUBLISHABLE_KEY:
        raise HTTPException(status_code=500, detail="Stripe publishable key not configured")
    return {"publishableKey": STRIPE_PUBLISHABLE_KEY}


@router.post("/checkout-sessions", status_code=status.HTTP_201_CREATED)
def create_checkout_session(body: CreateCheckoutSessionRequest):
    try:
        params = {
            "mode": body.mode,
            "success_url": f"{FRONTEND_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{FRONTEND_URL}/payment/cancel",
            "metadata": body.metadata or {},
        }
        if body.customer_id:
            params["customer"] = body.customer_id
        elif body.customer_email:
            params["customer_email"] = body.customer_email
        if body.line_items:
            params["line_items"] = body.line_items
        elif body.price_id:
            params["line_items"] = [{"price": body.price_id, "quantity": 1}]
        else:
            raise HTTPException(status_code=400, detail="Either price_id or line_items must be provided")
        session = stripe.checkout.Session.create(**params)
        return {"id": session.id, "url": session.url, "status": session.status, "mode": session.mode}
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