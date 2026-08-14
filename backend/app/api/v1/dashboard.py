"""
API Router: Dashboard
Aggregated data for the home dashboard.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ...core.auth import get_current_user, CurrentUser
from ...core.tenant import get_current_tenant
from ...models_orm import (
    CardCollection, BankMovement, ReconciliationResult, ReconciliationStatus,
    Institution, TPVClosingReport,
)
from ...schemas import DashboardSummaryResponse, CashFlowDashboardResponse

router = APIRouter(prefix="/dashboard")


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get dashboard key metrics (aggregates all historical data)."""
    today = date.today()

    # Total collections (all time)
    collections_query = select(func.sum(CardCollection.amount_gross)).where(
        CardCollection.tenant_id == tenant_id
    )
    today_total = await db.scalar(collections_query) or 0

    # Total collections up to yesterday for comparison
    yesterday_query = select(func.sum(CardCollection.amount_gross)).where(
        and_(CardCollection.tenant_id == tenant_id, CardCollection.collection_date < today)
    )
    yesterday_total = await db.scalar(yesterday_query) or 0

    # Cleared vs pending counts (all time)
    status_counts = await db.execute(
        select(
            CardCollection.status,
            func.count().label("count"),
            func.sum(CardCollection.amount_gross).label("total"),
        ).where(
            CardCollection.tenant_id == tenant_id
        ).group_by(CardCollection.status)
    )

    cleared = 0
    pending = 0
    for row in status_counts:
        status_val = row.status
        if status_val in (ReconciliationStatus.CLEARED.value, ReconciliationStatus.MATCHED.value):
            cleared = (cleared or 0) + (row.total or 0)
        elif status_val in (ReconciliationStatus.PENDING.value, ReconciliationStatus.PARTIAL.value, ReconciliationStatus.UNMATCHED.value, ReconciliationStatus.DISCREPANCY.value):
            pending = (pending or 0) + (row.total or 0)

    # Latest bank balance
    balance_query = select(BankMovement.balance_after).where(
        BankMovement.tenant_id == tenant_id
    ).order_by(BankMovement.booking_date.desc()).limit(1)
    latest_balance = await db.scalar(balance_query) or 0

    # Discrepancy count
    discrepancy_query = select(func.count()).where(
        and_(
            ReconciliationResult.tenant_id == tenant_id,
            ReconciliationResult.status == ReconciliationStatus.DISCREPANCY,
            ReconciliationResult.resolved == False,
        )
    )
    discrepancy_count = await db.scalar(discrepancy_query) or 0

    # Uncleared count
    uncleared_query = select(func.count()).where(
        and_(
            CardCollection.tenant_id == tenant_id,
            CardCollection.status != ReconciliationStatus.CLEARED,
            CardCollection.collection_date <= today,
        )
    )
    uncleared_count = await db.scalar(uncleared_query) or 0

    return DashboardSummaryResponse(
        today_collections=today_total,
        yesterday_collections=yesterday_total,
        change_percent=((today_total - yesterday_total) / yesterday_total * 100) if yesterday_total else 0,
        cleared_amount=cleared,
        pending_amount=pending,
        bank_balance=latest_balance,
        discrepancy_count=discrepancy_count,
        uncleared_count=uncleared_count,
    )


@router.get("/cash-flow", response_model=CashFlowDashboardResponse)
async def get_cash_flow_dashboard(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get cash flow data for dashboard chart."""
    from ...services.cashflow_service import CashFlowService

    service = CashFlowService(db, tenant_id)
    entries = await service.generate_cash_flow_dashboard(days)

    return CashFlowDashboardResponse(entries=entries)
