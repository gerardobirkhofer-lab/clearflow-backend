"""
API Router: Dashboard
Aggregated data for the home dashboard.
Reads from legacy tables (bank_transactions, provider_transactions) where
CSV uploads actually store data, with ORM tables as fallback.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ...core.auth import get_current_user, CurrentUser
from ...core.tenant import get_current_tenant
from ...models_orm import (
    ReconciliationResult, ReconciliationStatus,
)
from ...schemas import DashboardSummaryResponse, CashFlowDashboardResponse

# Legacy models where CSV data is actually stored
from ...models.bank_transaction import BankTransaction
from ...models.provider_transaction import ProviderTransaction

router = APIRouter(prefix="/dashboard")


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get dashboard key metrics from legacy transaction tables."""
    today = date.today()
    yesterday = today - timedelta(days=1)

    # --- Provider transactions (sales/collections) ALL TIME ---
    prov_total_query = select(func.sum(ProviderTransaction.amount)).where(
        ProviderTransaction.tenant_id == tenant_id
    )
    prov_total = await db.scalar(prov_total_query) or 0

    # --- Bank transactions ALL TIME ---
    bank_total_query = select(func.sum(BankTransaction.amount)).where(
        BankTransaction.tenant_id == tenant_id
    )
    bank_total = await db.scalar(bank_total_query) or 0

    bank_matched_query = select(func.sum(BankTransaction.amount)).where(
        and_(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 1,
        )
    )
    bank_matched = await db.scalar(bank_matched_query) or 0

    bank_unmatched_query = select(func.sum(BankTransaction.amount)).where(
        and_(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    )
    bank_unmatched = await db.scalar(bank_unmatched_query) or 0

    prov_matched_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 1,
        )
    )
    prov_matched = await db.scalar(prov_matched_query) or 0

    prov_unmatched_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_unmatched = await db.scalar(prov_unmatched_query) or 0
    prov_today_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            func.date(ProviderTransaction.transaction_date) == today,
        )
    )
    prov_today = await db.scalar(prov_today_query) or 0

    prov_yesterday_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            func.date(ProviderTransaction.transaction_date) == yesterday,
        )
    )
    prov_yesterday = await db.scalar(prov_yesterday_query) or 0

    bank_matched_query = select(func.sum(BankTransaction.amount)).where(
        and_(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 1,
        )
    )
    bank_matched = await db.scalar(bank_matched_query) or 0

    bank_unmatched_query = select(func.sum(BankTransaction.amount)).where(
        and_(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    )
    bank_unmatched = await db.scalar(bank_unmatched_query) or 0

    prov_matched_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 1,
        )
    )
    prov_matched = await db.scalar(prov_matched_query) or 0

    prov_unmatched_query = select(func.sum(ProviderTransaction.amount)).where(
        and_(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_unmatched = await db.scalar(prov_unmatched_query) or 0

    # Latest bank balance (from most recent bank transaction with balance)
    balance_query = select(BankTransaction.balance).where(
        and_(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.balance != None,
        )
    ).order_by(BankTransaction.transaction_date.desc()).limit(1)
    latest_balance_row = await db.execute(balance_query)
    latest_balance_scalar = latest_balance_row.scalar_one_or_none()
    latest_balance = latest_balance_scalar or 0

    # Discrepancy count from reconciliation results
    discrepancy_query = select(func.count()).where(
        and_(
            ReconciliationResult.tenant_id == tenant_id,
            ReconciliationResult.status == ReconciliationStatus.DISCREPANCY,
            ReconciliationResult.resolved == False,
        )
    )
    discrepancy_count = await db.scalar(discrepancy_query) or 0

    # Uncleared count = unmatched transactions
    uncleared_bank = await db.scalar(
        select(func.count()).where(
            and_(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.matched == 0,
            )
        )
    ) or 0
    uncleared_provider = await db.scalar(
        select(func.count()).where(
            and_(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.matched == 0,
            )
        )
    ) or 0
    uncleared_count = int(uncleared_bank) + int(uncleared_provider)

    today_collections = abs(float(prov_total))
    yesterday_collections = abs(float(bank_total))
    cleared_amount = abs(float(bank_matched)) + abs(float(prov_matched))
    pending_amount = abs(float(bank_unmatched)) + abs(float(prov_unmatched))

    total_all = today_collections + yesterday_collections
    cleared_pct = (cleared_amount / total_all * 100) if total_all else 0

    return DashboardSummaryResponse(
        today_collections=today_collections,
        yesterday_collections=yesterday_collections,
        change_percent=cleared_pct,
        cleared_amount=cleared_amount,
        pending_amount=pending_amount,
        bank_balance=abs(float(latest_balance)),
        discrepancy_count=discrepancy_count,
        uncleared_count=uncleared_count,
    )
    yesterday_collections = abs(float(prov_yesterday))
    cleared_amount = abs(float(bank_matched)) + abs(float(prov_matched))
    pending_amount = abs(float(bank_unmatched)) + abs(float(prov_unmatched))

    return DashboardSummaryResponse(
        today_collections=today_collections,
        yesterday_collections=yesterday_collections,
        change_percent=((today_collections - yesterday_collections) / yesterday_collections * 100) if yesterday_collections else 0,
        cleared_amount=cleared_amount,
        pending_amount=pending_amount,
        bank_balance=abs(float(latest_balance)),
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
