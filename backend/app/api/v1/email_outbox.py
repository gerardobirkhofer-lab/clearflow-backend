"""
Email outbox — drafts require client approval before send.
Reject = ClearFlow work done; we do not interfere.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_shared_db
from app.core.email import get_email_service
from app.core.tenant import assert_tenant_access
from app.models.dispute_email_log import DisputeEmailLog
from app.models.email_outbox import EmailOutbox

router = APIRouter()


def _serialize(item: EmailOutbox) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "dispute_id": item.dispute_id,
        "provider_name": item.provider_name,
        "to_email": item.to_email,
        "subject": item.subject,
        "body_text": item.body_text,
        "body_html": item.body_html,
        "amount": item.amount,
        "currency": item.currency,
        "concept": item.concept,
        "needs_recipient": bool(item.needs_recipient),
        "created_by": item.created_by,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "rejected_at": item.rejected_at.isoformat() if item.rejected_at else None,
        "sent_at": item.sent_at.isoformat() if item.sent_at else None,
        "error_message": item.error_message,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "policy": "client_must_approve_before_send",
    }


async def _get_item(
    db: AsyncSession, tenant_id: uuid.UUID, item_id: int
) -> EmailOutbox:
    row = await db.execute(
        select(EmailOutbox).where(
            EmailOutbox.id == item_id,
            EmailOutbox.tenant_id == tenant_id,
        )
    )
    item = row.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Outbox item not found")
    return item


@router.get("/")
async def list_outbox(
    tenant_id: uuid.UUID = Query(...),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    q = select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_id)
    if status:
        q = q.where(EmailOutbox.status == status)
    q = q.order_by(EmailOutbox.id.desc()).limit(200)
    rows = (await db.execute(q)).scalars().all()
    pending = sum(1 for r in rows if r.status == "pending_approval")
    return {
        "items": [_serialize(r) for r in rows],
        "pending_approval_count": pending,
        "note": (
            "ClearFlow genera el borrador; el cliente lee y Autoriza o Rechaza. "
            "Sin autorización no se envía nada al proveedor."
        ),
    }


@router.get("/{item_id}")
async def get_outbox_item(
    item_id: int,
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    return _serialize(await _get_item(db, tenant_id, item_id))


@router.post("/{item_id}/approve")
async def approve_and_send(
    item_id: int,
    tenant_id: uuid.UUID = Query(...),
    to_email: str | None = Query(None, description="Override recipient if missing"),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Client authorization gate: approve = authorize send.
    Only then SMTP is attempted.
    """
    tenant_id = assert_tenant_access(tenant_id, current_user)
    item = await _get_item(db, tenant_id, item_id)
    if item.status in ("sent", "cancelled", "rejected"):
        raise HTTPException(status_code=400, detail=f"Cannot approve status={item.status}")

    if to_email:
        item.to_email = to_email.strip()
        item.needs_recipient = 0
    if not item.to_email:
        raise HTTPException(
            status_code=400,
            detail="Falta email del proveedor. Configúralo en Setup o pásalo en to_email.",
        )

    item.status = "approved"
    item.approved_at = datetime.utcnow()
    await db.flush()

    email_service = get_email_service()
    result = await email_service.send_email(
        to=item.to_email,
        subject=item.subject,
        body_text=item.body_text,
        body_html=item.body_html,
    )

    log = DisputeEmailLog(
        tenant_id=tenant_id,
        provider_name=item.provider_name or "provider",
        amount=item.amount or 0,
        currency=item.currency or "EUR",
        concept=item.concept or "",
        description=item.body_text[:2000],
        recipient_email=item.to_email,
        status="sent" if result.get("success") else "failed",
        error_message=None if result.get("success") else str(result.get("error") or "send_failed"),
    )
    db.add(log)

    if result.get("success"):
        item.status = "sent"
        item.sent_at = datetime.utcnow()
        item.error_message = None
    else:
        item.status = "failed"
        item.error_message = str(result.get("error") or "SMTP failed")

    await db.commit()
    await db.refresh(item)
    return {
        "item": _serialize(item),
        "smtp": result,
        "message": (
            "Email enviado tras tu autorización"
            if item.status == "sent"
            else "Autorizado pero el envío SMTP falló — revisa SMTP o reintenta"
        ),
    }


@router.post("/{item_id}/reject")
async def reject_draft(
    item_id: int,
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Client declines to send. ClearFlow work is done; we do not interfere."""
    tenant_id = assert_tenant_access(tenant_id, current_user)
    item = await _get_item(db, tenant_id, item_id)
    if item.status == "sent":
        raise HTTPException(status_code=400, detail="Already sent")
    item.status = "rejected"
    item.rejected_at = datetime.utcnow()
    await db.commit()
    return {
        "item": _serialize(item),
        "message": (
            "Rechazado. ClearFlow no enviará nada. "
            "La discrepancia sigue documentada en el dashboard; es decisión del cliente."
        ),
    }


@router.post("/{item_id}/cancel")
async def cancel_draft(
    item_id: int,
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    item = await _get_item(db, tenant_id, item_id)
    if item.status == "sent":
        raise HTTPException(status_code=400, detail="Already sent")
    item.status = "cancelled"
    await db.commit()
    return {"item": _serialize(item), "message": "Cancelled"}
