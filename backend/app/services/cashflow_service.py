"""
CashFlow Service — real implementation from legacy transaction tables.
Generates daily cash flow entries from bank_transactions and provider_transactions.
"""
from __future__ import annotations
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from ..models.bank_transaction import BankTransaction
from ..models.provider_transaction import ProviderTransaction


class CashFlowService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def generate_cash_flow_dashboard(self, days: int = 30):
        """Generate daily cash flow entries for the last N days."""
        end_date = date.today()
        entries = []

        for i in range(days + 1):
            day = end_date - timedelta(days=i)

            # Bank inflows for this day
            bank_result = await self.db.execute(
                select(func.sum(BankTransaction.amount)).where(
                    and_(
                        BankTransaction.tenant_id == self.tenant_id,
                        func.date(BankTransaction.transaction_date) == day,
                    )
                )
            )
            bank_amount = bank_result.scalar_one_or_none() or 0

            # Provider sales for this day
            prov_result = await self.db.execute(
                select(func.sum(ProviderTransaction.amount)).where(
                    and_(
                        ProviderTransaction.tenant_id == self.tenant_id,
                        func.date(ProviderTransaction.transaction_date) == day,
                    )
                )
            )
            prov_amount = prov_result.scalar_one_or_none() or 0

            # Only add days with activity
            if bank_amount != 0 or prov_amount != 0:
                entries.append({
                    "date": day.isoformat(),
                    "bank_inflow": abs(float(bank_amount)),
                    "provider_sales": abs(float(prov_amount)),
                    "net_flow": abs(float(bank_amount)) - abs(float(prov_amount)),
                })

        # Sort by date ascending (oldest first)
        entries.sort(key=lambda x: x["date"])
        return entries
