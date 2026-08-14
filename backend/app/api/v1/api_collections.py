"""
API Router: Card Collections
Handles upload, listing, filtering, and status updates for daily card collections.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.auth import get_current_user, require_role, CurrentUser
from ..core.tenant import get_current_tenant
from ..models_orm import CardCollection, Institution, ReconciliationStatus, TransactionType
from ..schemas import (
    CardCollectionCreate,
    CardCollectionResponse,
    CardCollectionListResponse,
    CollectionUploadResult,
)

router = APIRouter(prefix="/collections")


@router.get("", response_model=CardCollectionListResponse)
async def list_collections(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    institution_id: UUID | None = Query(None),
    status: ReconciliationStatus | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """List card collections with filtering and pagination."""
    query = select(CardCollection).where(CardCollection.tenant_id == tenant_id)

    if start_date:
        query = query.where(CardCollection.collection_date >= start_date)
    if end_date:
        query = query.where(CardCollection.collection_date <= end_date)
    if institution_id:
        query = query.where(CardCollection.institution_id == institution_id)
    if status:
        query = query.where(CardCollection.status == status)
    if search:
        query = query.where(CardCollection.reference.ilike(f"%{search}%"))

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query)

    # Paginate
    query = query.order_by(CardCollection.collection_date.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    collections = result.scalars().all()

    return CardCollectionListResponse(
        items=collections,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/manual", response_model=CardCollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection_manual(
    data: CardCollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin", "accountant"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Create a single collection manually (e.g., from web form)."""
    # Verify institution belongs to tenant
    inst = await db.get(Institution, data.institution_id)
    if not inst or inst.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Institution not found")

    collection = CardCollection(
        tenant_id=tenant_id,
        collection_date=data.collection_date,
        institution_id=data.institution_id,
        reference=data.reference,
        amount_gross=data.amount_gross,
        card_type=data.card_type,
        terminal_id=data.terminal_id,
        batch_number=data.batch_number,
        description=data.description,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    return collection


@router.post("/upload", response_model=CollectionUploadResult)
async def upload_collections_csv(
    file: UploadFile = File(...),
    institution_id: UUID = ...,
    date_format: str = Query("%Y-%m-%d"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin", "accountant"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Upload a CSV of collections. Returns a job ID for async processing."""
    # Verify institution
    inst = await db.get(Institution, institution_id)
    if not inst or inst.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Institution not found")

    # Validate file type
    if not file.filename.endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only CSV and Excel files are supported")

    # Read and parse CSV
    content = await file.read()
    decoded = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded))

    collections = []
    errors = []

    for row_num, row in enumerate(reader, start=2):
        try:
            collection = CardCollection(
                tenant_id=tenant_id,
                collection_date=datetime.strptime(row["date"], date_format).date(),
                institution_id=institution_id,
                reference=row.get("reference", ""),
                amount_gross=Decimal(row["amount_gross"]),
                card_type=TransactionType(row.get("card_type", "debit")),
                terminal_id=row.get("terminal_id"),
                batch_number=row.get("batch_number"),
                card_last_digits=row.get("card_last_digits"),
                transaction_count=int(row.get("transaction_count", 1)),
                description=row.get("description"),
                raw_data=dict(row),
            )
            collections.append(collection)
        except (KeyError, ValueError, Exception) as e:
            errors.append({"row": row_num, "error": str(e), "data": dict(row)})

    # Bulk insert valid collections
    db.add_all(collections)
    await db.commit()

    # Refresh to get IDs
    for c in collections:
        await db.refresh(c)

    return CollectionUploadResult(
        job_id=str(uuid.uuid4()),
        total_rows=len(collections) + len(errors),
        processed=len(collections),
        failed=len(errors),
        errors=errors[:10],  # Return first 10 errors
        collections=[CardCollectionResponse.from_orm(c) for c in collections[:5]],
    )


@router.get("/{collection_id}", response_model=CardCollectionResponse)
async def get_collection(
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get a single collection by ID."""
    collection = await db.get(CardCollection, collection_id)
    if not collection or collection.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Collection not found")
    return collection


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Delete a collection (use with caution — may break reconciliation)."""
    collection = await db.get(CardCollection, collection_id)
    if not collection or collection.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Collection not found")

    await db.delete(collection)
    await db.commit()
    return None
