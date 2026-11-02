"""Tenant document vault API — ClearFlow-hosted default; BYO S3-compatible."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant import assert_tenant_access
from app.models.tenant_document import TenantDocument
from app.models.tenant_vault_settings import TenantVaultSettings
from app.services import secret_box, vault_storage

router = APIRouter()

MAX_BYTES = 15 * 1024 * 1024


async def _vault_settings(
    db: AsyncSession, tenant_id: uuid.UUID
) -> TenantVaultSettings | None:
    row = await db.execute(
        select(TenantVaultSettings).where(TenantVaultSettings.tenant_id == tenant_id)
    )
    return row.scalar_one_or_none()


def _s3_cfg_from_settings(s: TenantVaultSettings) -> dict:
    return {
        "endpoint": s.s3_endpoint,
        "bucket": s.s3_bucket,
        "region": s.s3_region or "eu-west-1",
        "access_key": secret_box.decrypt_str(s.s3_access_key_enc),
        "secret_key": secret_box.decrypt_str(s.s3_secret_key_enc),
        "secure": True,
    }


@router.get("/storage-config")
async def get_storage_config(
    tenant_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    s = await _vault_settings(db, tenant_id)
    backend = (s.backend if s else "clearflow_local")
    return {
        "choice": "byo" if backend == "s3" else "clearflow",
        "backend": backend,
        "s3": {
            "endpoint": s.s3_endpoint if s else None,
            "bucket": s.s3_bucket if s else None,
            "region": s.s3_region if s else None,
            "configured": bool(s and s.s3_access_key_enc and s.s3_bucket),
        }
        if s
        else None,
        "backends": {
            "clearflow": {
                "available": True,
                "label": "Vault ClearFlow",
                "note": "Almacén por tenant incluido. Recomendado para empezar.",
            },
            "byo": {
                "available": True,
                "status": "s3_compatible",
                "label": "Tu propia nube (S3 / MinIO / R2 / GCS S3 API)",
                "note": "Tú controlas el bucket. ClearFlow solo lee/escribe documentos del tenant.",
            },
        },
    }


@router.put("/storage-config")
async def put_storage_config(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Body: {
      tenant_id, backend: clearflow_local|s3,
      s3_endpoint?, s3_bucket?, s3_region?,
      s3_access_key?, s3_secret_key?
    }
    """
    raw_tid = payload.get("tenant_id")
    if not raw_tid:
        raise HTTPException(status_code=400, detail="tenant_id required")
    tenant_id = assert_tenant_access(uuid.UUID(str(raw_tid)), current_user)
    backend = (payload.get("backend") or "clearflow_local").strip()
    if backend not in ("clearflow_local", "s3"):
        raise HTTPException(status_code=400, detail="backend must be clearflow_local or s3")

    s = await _vault_settings(db, tenant_id)
    if not s:
        s = TenantVaultSettings(tenant_id=tenant_id)
        db.add(s)

    s.backend = backend
    if backend == "s3":
        s.s3_endpoint = (payload.get("s3_endpoint") or "").strip() or None
        s.s3_bucket = (payload.get("s3_bucket") or "").strip() or None
        s.s3_region = (payload.get("s3_region") or "eu-west-1").strip()
        if payload.get("s3_access_key"):
            s.s3_access_key_enc = secret_box.encrypt_str(str(payload["s3_access_key"]))
        if payload.get("s3_secret_key"):
            s.s3_secret_key_enc = secret_box.encrypt_str(str(payload["s3_secret_key"]))
        if not s.s3_endpoint or not s.s3_bucket or not s.s3_access_key_enc:
            raise HTTPException(
                status_code=400,
                detail="s3_endpoint, s3_bucket and s3_access_key required for BYO",
            )
    await db.commit()
    return {
        "message": "Vault storage preference saved",
        "backend": s.backend,
        "choice": "byo" if s.backend == "s3" else "clearflow",
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    tenant_id: uuid.UUID = Form(...),
    category: str = Form("other"),
    related_provider_name: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 15MB)")

    cat = (category or "other").strip().lower()[:40]
    settings = await _vault_settings(db, tenant_id)
    backend = settings.backend if settings else "clearflow_local"
    s3_cfg = _s3_cfg_from_settings(settings) if backend == "s3" and settings else None
    try:
        stored = vault_storage.store_bytes(
            tenant_id,
            filename=file.filename or "document",
            data=data,
            category=cat,
            backend=backend,
            s3_cfg=s3_cfg,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vault write failed: {e}") from e

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
        "message": "Documento guardado en el vault del tenant.",
    }


@router.get("/")
async def list_documents(
    tenant_id: uuid.UUID = Query(...),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
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
    settings = await _vault_settings(db, tenant_id)
    s3_cfg = (
        _s3_cfg_from_settings(settings)
        if doc.storage_backend == "s3" and settings
        else None
    )
    try:
        data = vault_storage.read_bytes(
            doc.storage_key, backend=doc.storage_backend, s3_cfg=s3_cfg
        )
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
    db: AsyncSession = Depends(get_db),
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
