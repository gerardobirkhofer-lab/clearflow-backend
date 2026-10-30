"""
Webhook receivers for third-party providers.

- GoCardless: POST /api/v1/webhooks/gocardless
- Redsys / TPV online notification: POST /api/v1/webhooks/redsys
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_shared_db
from app.models.provider_transaction import ProviderTransaction
from app.services.redsys import (
    decode_merchant_parameters,
    summarize_operation,
    verify_redsys_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter()

GOCARDLESS_WEBHOOK_SECRET = os.getenv("GOCARDLESS_WEBHOOK_SECRET", "").strip()
REDSYS_SECRET_KEY = os.getenv("REDSYS_SECRET_KEY", "").strip()  # clave comercio (base64)
DEFAULT_TENANT_ID = os.getenv(
    "DEFAULT_TENANT_ID",
    "22222222-2222-2222-2222-222222222222",
)


def _signatures_match(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Validate GoCardless Webhook-Signature (one or more hex digests, space-separated)."""
    if not signature_header:
        return False
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = [part.strip() for part in signature_header.split() if part.strip()]
    return any(hmac.compare_digest(expected, sig) for sig in provided)


@router.get("/webhooks/gocardless")
async def gocardless_webhook_info() -> dict[str, Any]:
    """Browser/health check so you can confirm the public URL exists before saving in GoCardless."""
    return {
        "ok": True,
        "service": "clearflow",
        "endpoint": "/api/v1/webhooks/gocardless",
        "method": "POST",
        "signature_required": bool(GOCARDLESS_WEBHOOK_SECRET),
        "provider": "gocardless",
    }


@router.post("/webhooks/gocardless")
async def gocardless_webhook(
    request: Request,
    webhook_signature: str | None = Header(default=None, alias="Webhook-Signature"),
) -> JSONResponse:
    """
    Receive GoCardless webhook events.

    Paste this public URL in the GoCardless dashboard:
      https://<your-api-host>/api/v1/webhooks/gocardless
    Then set GOCARDLESS_WEBHOOK_SECRET to the endpoint secret GoCardless shows you.
    """
    body = await request.body()

    if GOCARDLESS_WEBHOOK_SECRET:
        if not _signatures_match(body, webhook_signature, GOCARDLESS_WEBHOOK_SECRET):
            logger.warning("gocardless_webhook_invalid_signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid GoCardless webhook signature",
            )
    else:
        logger.warning(
            "gocardless_webhook_secret_missing — accepting without verification; "
            "set GOCARDLESS_WEBHOOK_SECRET after creating the endpoint in GoCardless"
        )

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            logger.info(
                "gocardless_event id=%s resource_type=%s action=%s",
                event.get("id"),
                event.get("resource_type"),
                event.get("action"),
            )
        event_count = len(events)
    else:
        logger.info(
            "gocardless_webhook_payload keys=%s",
            list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        event_count = 1 if payload else 0

    return JSONResponse(
        status_code=200,
        content={
            "received": True,
            "events_processed": event_count,
        },
    )


# ── Redsys / TPV ──────────────────────────────────────────────────────────────


@router.get("/webhooks/redsys")
async def redsys_webhook_info() -> dict[str, Any]:
    """Confirm the public Redsys notification URL exists."""
    return {
        "ok": True,
        "service": "clearflow",
        "endpoint": "/api/v1/webhooks/redsys",
        "method": "POST",
        "signature_required": bool(REDSYS_SECRET_KEY),
        "provider": "redsys",
        "notes": (
            "Configure this URL as 'URL de notificación online' in the Redsys / bank TPV admin. "
            "Set REDSYS_SECRET_KEY to the merchant secret key (clave de comercio, base64)."
        ),
    }


def _extract_redsys_fields(form: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Pull Ds_* fields from form/json (case-tolerant)."""
    lower = {str(k).lower(): v for k, v in form.items()}

    def _one(key: str) -> str | None:
        val = lower.get(key.lower())
        if isinstance(val, list):
            val = val[0] if val else None
        return str(val) if val is not None else None

    return (
        _one("Ds_MerchantParameters"),
        _one("Ds_Signature"),
        _one("Ds_SignatureVersion"),
    )


@router.post("/webhooks/redsys")
async def redsys_webhook(
    request: Request,
    db: AsyncSession = Depends(get_shared_db),
):
    """
    Redsys online payment notification (notificación HTTP).

    Public URL for bank / Redsys admin:
      https://clearflow-api-production.up.railway.app/api/v1/webhooks/redsys

    Accepts application/x-www-form-urlencoded (standard) or JSON.
    Responds with plain 'OK' so Redsys stops retrying.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    body = await request.body()

    form: dict[str, Any] = {}
    if "application/json" in content_type:
        try:
            form = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    else:
        # form-urlencoded or multipart-ish query string
        try:
            text = body.decode("utf-8")
            parsed = parse_qs(text, keep_blank_values=True)
            form = {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}
        except Exception:
            form = {}

    merchant_params_b64, signature_b64, sig_version = _extract_redsys_fields(form)

    if not merchant_params_b64:
        logger.warning("redsys_webhook_missing_merchant_parameters keys=%s", list(form.keys()))
        raise HTTPException(status_code=400, detail="Missing Ds_MerchantParameters")

    try:
        params = decode_merchant_parameters(merchant_params_b64)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redsys_webhook_bad_parameters: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Ds_MerchantParameters") from exc

    if REDSYS_SECRET_KEY:
        if not signature_b64 or not verify_redsys_signature(
            merchant_params_b64, signature_b64, REDSYS_SECRET_KEY
        ):
            logger.warning(
                "redsys_webhook_invalid_signature order=%s version=%s",
                params.get("Ds_Order"),
                sig_version,
            )
            raise HTTPException(status_code=401, detail="Invalid Redsys signature")
    else:
        logger.warning(
            "redsys_secret_missing — accepting without verification; "
            "set REDSYS_SECRET_KEY (clave de comercio) ASAP"
        )

    summary = summarize_operation(params)
    logger.info(
        "redsys_notification order=%s amount=%s authorized=%s response=%s",
        summary.get("order"),
        summary.get("amount"),
        summary.get("authorized"),
        summary.get("response"),
    )

    # Persist authorized (and refunds/cancellations) for reconciliation
    reference = str(summary.get("order") or params.get("Ds_Order") or "")
    if reference:
        try:
            tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
        except ValueError:
            tenant_id = None

        if tenant_id is not None:
            existing = await db.execute(
                select(ProviderTransaction).where(
                    ProviderTransaction.tenant_id == tenant_id,
                    ProviderTransaction.provider_name == "redsys",
                    ProviderTransaction.reference == reference,
                )
            )
            if existing.scalar_one_or_none() is None:
                amount = float(summary.get("amount") or 0)
                # Non-authorized responses kept for audit with matched=0; amount may still help
                concept = (
                    f"Redsys OK {reference}"
                    if summary.get("authorized")
                    else f"Redsys Resp {summary.get('response')} {reference}"
                )
                tx = ProviderTransaction(
                    tenant_id=tenant_id,
                    provider_name="redsys",
                    amount=amount,
                    transaction_date=datetime.utcnow(),
                    concept=concept,
                    reference=reference,
                    matched=0,
                    raw_data=json.dumps({"params": params, "summary": summary}),
                )
                db.add(tx)
                await db.commit()

    # Redsys expects a simple OK body on success
    return PlainTextResponse("OK", status_code=200)
