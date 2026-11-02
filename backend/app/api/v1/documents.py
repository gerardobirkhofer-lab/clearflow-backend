"""Tenant document vault API — ClearFlow-hosted default; BYO stub."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_shared_db
from app.core.tenant import assert_tenant_access
from app.models.tenant_document import TenantDocument
from app.services import vault_storage

router = APIRouter()

MAX_BYTES = 15 * 1024 * 1024  # 15 MB


@router.get("/storage-config")
async def get_storage_config(
    tenant_id: uuid.UUID = Query(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    assert_tenant_access(tenant_id, current_user)
    # Preference is client-side for now; API documents the policy
    return {
        "choice": "clearflow",
        "backends": {
            "clearflow": {
                "available": True,
                "label": "Vault ClearFlow",
                "note": "Almacén cifrado por tenant, incluido en el plan. Recomendado para empezar.",
            },
            "byo": {
                "available": False,
                "status": "stub",
                "label": "Tu propia nube",
                "note": (
                    "Puedes conectar S3 / Azure / GCS / Drive más adelante. "
                    "No hace falta contratar nada extra para arrancar."
                ),
            },
        },
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    category: str = Form("other"),
    related_provider_name: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 15MB)")

    cat = (category or "other").strip().lower()[:40]
    stored = vault_storage.store_bytes(
        tenant_id,
        filename=file.filename or "document",
        data=data,
        category=cat,
    )
    doc = TenantDocument(
        tenant_id=tenant_id,
        category=cat,
        original_filename=(file.filename or "document")[:255],
        content_type=file.content_type,
        size_bytes=stored["size_bytes"],
        storage_backend=stored["storage_backend"],
        storage_key=stored["storage_key"],
        checksum_sha256=stored["checksum_sha256"],
        related_provider_name=(related_provider_name or None),
        notes=(notes or None),
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return {
        "id": doc.id,
        "filename": doc.original_filename,
        "category": doc.category,
        "size_bytes": doc.size_bytes,
        "storage_backend": doc.storage_backend,
        "related_provider_name": doc.related_provider_name,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "message": "Documento guardado en el vault ClearFlow del tenant.",
    }


@router.get("/")
async def list_documents(
    tenant_id: uuid.UUID = Query(...),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    q = select(TenantDocument).where(
        TenantDocument.tenant_id == tenant_id,
        TenantDocument.deleted_at.is_(None),
    )
    if category:
        q = q.where(TenantDocument.category == category)
    q = q.order_by(TenantDocument.id.desc()).limit(200)
    rows = (await db.execute(q)).scalars().all()
    return {
        "documents": [
            {
                "id": d.id,
                "filename": d.original_filename,
                "category": d.category,
                "size_bytes": d.size_bytes,
                "content_type": d.content_type,
                "storage_backend": d.storage_backend,
                "related_provider_name": d.related_provider_name,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ]
    }


@router.get("/{doc_id}/download")
async def download_document(
    doc_id: int,
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    row = await db.execute(
        select(TenantDocument).where(
            TenantDocument.id == doc_id,
            TenantDocument.tenant_id == tenant_id,
            TenantDocument.deleted_at.is_(None),
        )
    )
    doc = row.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        data = vault_storage.read_bytes(doc.storage_key)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File missing: {e}") from e
    return Response(
        content=data,
        media_type=doc.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{doc.original_filename}"'
        },
    )


@router.delete("/{doc_id}")
async def soft_delete_document(
    doc_id: int,
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_shared_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    row = await db.execute(
        select(TenantDocument).where(
            TenantDocument.id == doc_id,
            TenantDocument.tenant_id == tenant_id,
            TenantDocument.deleted_at.is_(None),
        )
    )
    doc = row.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    doc.deleted_at = datetime.utcnow()
    await db.commit()
    return {"message": "Document deleted", "id": doc_id}
