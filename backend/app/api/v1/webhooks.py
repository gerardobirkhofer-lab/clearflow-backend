"""
Webhook receivers for third-party providers.

GoCardless (Payments / Bank Account Data events):
  POST /api/v1/webhooks/gocardless

Verification uses HMAC-SHA256 of the raw body against GOCARDLESS_WEBHOOK_SECRET,
compared to the Webhook-Signature header (GoCardless standard).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

GOCARDLESS_WEBHOOK_SECRET = os.getenv("GOCARDLESS_WEBHOOK_SECRET", "").strip()


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
        # Bootstrap mode: accept so the URL can be registered; enable secret ASAP.
        logger.warning(
            "gocardless_webhook_secret_missing — accepting without verification; "
            "set GOCARDLESS_WEBHOOK_SECRET after creating the endpoint in GoCardless"
        )

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    # GoCardless Payments sends {"events": [...]}; Bank Account Data may send other shapes.
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

    # Always 200 quickly so GoCardless does not retry; processing can be expanded later.
    return JSONResponse(
        status_code=200,
        content={
            "received": True,
            "events_processed": event_count,
        },
    )
