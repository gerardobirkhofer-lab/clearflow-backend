"""
Build dispute email drafts for client pre-approval. Never auto-sends.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_outbox import EmailOutbox
from app.models.provider import Provider


def build_dispute_email_bodies(
    *,
    provider_name: str,
    dispute_id: int | None,
    dispute_type: str,
    amount: float,
    description: str,
    age_days: int = 0,
) -> dict[str, str]:
    ref = f"#{dispute_id}" if dispute_id else "(pendiente)"
    subject = (
        f"[{provider_name}] Discrepancia ClearFlow {ref} — revisión de liquidación"
    )
    body_text = f"""CLEARFLOW — INFORME DE DISCREPANCIA (borrador para autorización del cliente)

Proveedor: {provider_name}
Dispute ID: {ref}
Fecha informe: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

═══════════════════════════════════════════════════════════════
DETALLE
═══════════════════════════════════════════════════════════════
Tipo: {dispute_type}
Importe: €{amount:.2f}
Días abiertos: {age_days}

Descripción:
{description or 'N/A'}

═══════════════════════════════════════════════════════════════
ACCIÓN SOLICITADA
═══════════════════════════════════════════════════════════════
Rogamos revisen y acrediten el importe pendiente en el próximo
ciclo de liquidación. Referencien Dispute ID {ref} en su respuesta.

Atentamente,
ClearFlow — Control de caja (en nombre del comercio, tras su autorización)
"""
    body_html = f"""<html><body style="font-family:Arial,sans-serif;color:#333;line-height:1.6;">
<h2 style="color:#635bff;">ClearFlow — Informe de discrepancia</h2>
<p><em>Borrador — solo se envía tras autorización del cliente.</em></p>
<table style="border-collapse:collapse;width:100%;max-width:600px;">
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Proveedor</strong></td><td>{provider_name}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Tipo</strong></td><td>{dispute_type}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Importe</strong></td><td>€{amount:.2f}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><strong>Ref.</strong></td><td>{ref}</td></tr>
</table>
<p><strong>Descripción:</strong><br/>{description or 'N/A'}</p>
<p style="background:#f0fdf4;padding:12px;border-radius:8px;">
<strong>Acción solicitada:</strong> revisar y acreditar el importe en el próximo ciclo.
</p>
<p>ClearFlow (envio autorizado por el comercio)</p>
</body></html>"""
    return {"subject": subject, "body_text": body_text, "body_html": body_html}


async def enqueue_dispute_email_draft(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    dispute_id: int | None,
    provider_name: str,
    dispute_type: str,
    amount: float,
    description: str,
    meta: dict[str, Any] | None = None,
) -> EmailOutbox:
    to_email = None
    pname = (provider_name or "").strip()
    if pname:
        row = await db.execute(
            select(Provider).where(
                Provider.tenant_id == tenant_id,
                Provider.is_active == 1,
            )
        )
        for p in row.scalars().all():
            n = (p.name or "").lower()
            t = (p.provider_type or "").lower()
            if n == pname.lower() or t == pname.lower() or pname.lower() in n or n in pname.lower():
                to_email = p.dispute_email
                break

    bodies = build_dispute_email_bodies(
        provider_name=pname or "provider",
        dispute_id=dispute_id,
        dispute_type=dispute_type,
        amount=float(amount or 0),
        description=description or "",
    )
    item = EmailOutbox(
        tenant_id=tenant_id,
        kind="provider_dispute",
        status="pending_approval",
        dispute_id=dispute_id,
        provider_name=pname[:100] if pname else None,
        to_email=to_email,
        subject=bodies["subject"][:500],
        body_text=bodies["body_text"],
        body_html=bodies["body_html"],
        amount=float(amount or 0),
        concept=dispute_type,
        meta_json=json.dumps(meta or {}, ensure_ascii=False)[:4000],
        created_by="system",
        needs_recipient=0 if to_email else 1,
    )
    db.add(item)
    return item
