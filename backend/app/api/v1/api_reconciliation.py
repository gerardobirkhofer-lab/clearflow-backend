"""
API Router: Reconciliation
Triggers matching engine and returns results.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.auth import get_current_user, require_role, CurrentUser
from ..core.tenant import get_current_tenant
from ..models_orm import ReconciliationResult, ReconciliationStatus
from ..schemas import (
    ReconciliationRunRequest,
    ReconciliationRunResponse,
    ReconciliationResultResponse,
    ReconciliationResultListResponse,
    ManualResolutionRequest,
)
from ..services.reconciliation_service import ReconciliationService

router = APIRouter(prefix="/reconciliation")


@router.post("/run", response_model=ReconciliationRunResponse)
async def run_reconciliation(
    request: ReconciliationRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin", "accountant"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Trigger reconciliation for a date range. If no dates provided, reconciles yesterday."""
    if request is None:
        request = ReconciliationRunRequest()

    if request.target_date is None:
        request.target_date = date.today() - timedelta(days=1)

    service = ReconciliationService(db, tenant_id)
    results = await service.run_reconciliation(
        target_date=request.target_date,
        institution_id=request.institution_id,
        dry_run=request.dry_run,
    )

    return ReconciliationRunResponse(
        target_date=request.target_date,
        total_processed=len(results),
        results=results,
        dry_run=request.dry_run,
    )


@router.get("/results", response_model=ReconciliationResultListResponse)
async def list_results(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    status: ReconciliationStatus | None = Query(None),
    institution_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """List reconciliation results with filtering."""
    query = select(ReconciliationResult).where(ReconciliationResult.tenant_id == tenant_id)

    if start_date:
        query = query.where(ReconciliationResult.collection_date >= start_date)
    if end_date:
        query = query.where(ReconciliationResult.collection_date <= end_date)
    if status:
        query = query.where(ReconciliationResult.status == status)
    if institution_id:
        # Join through collection to filter by institution
        from ..models_orm import CardCollection
        query = query.join(CardCollection).where(CardCollection.institution_id == institution_id)

    query = query.order_by(ReconciliationResult.collection_date.desc())

    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    results = result.scalars().all()

    return ReconciliationResultListResponse(
        items=results,
        page=page,
        page_size=page_size,
    )


@router.get("/uncleared", response_model=ReconciliationResultListResponse)
async def get_uncleared(
    as_of: date | None = Query(None),
    institution_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get all uncleared collections (not fully matched/cleared)."""
    if as_of is None:
        as_of = date.today()

    service = ReconciliationService(db, tenant_id)
    results = await service.get_uncleared_results(as_of, institution_id)

    return ReconciliationResultListResponse(
        items=results,
        page=1,
        page_size=len(results),
    )


@router.get("/discrepancies", response_model=ReconciliationResultListResponse)
async def get_discrepancies(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get all active discrepancies requiring attention."""
    query = select(ReconciliationResult).where(
        and_(
            ReconciliationResult.tenant_id == tenant_id,
            ReconciliationResult.status == ReconciliationStatus.DISCREPANCY,
            ReconciliationResult.resolved == False,
        )
    ).order_by(ReconciliationResult.collection_date.desc())

    result = await db.execute(query)
    discrepancies = result.scalars().all()

    return ReconciliationResultListResponse(
        items=discrepancies,
        page=1,
        page_size=len(discrepancies),
    )


@router.post("/{result_id}/resolve", response_model=ReconciliationResultResponse)
async def resolve_manually(
    result_id: UUID,
    request: ManualResolutionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_role(["owner", "admin", "accountant"])),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Manually resolve a reconciliation result (e.g., mark as cleared after investigation)."""
    service = ReconciliationService(db, tenant_id)
    result = await service.resolve_manually(
        result_id=result_id,
        status=request.status,
        notes=request.notes,
        resolved_by=current_user.id,
    )

    if not result:
        raise HTTPException(status_code=404, detail="Reconciliation result not found")

    return result
