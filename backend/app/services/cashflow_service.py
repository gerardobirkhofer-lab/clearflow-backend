"""
CashFlow Service Stub — replace with real implementation.
"""
from __future__ import annotations
from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class CashFlowService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def generate_cash_flow_dashboard(self, days: int = 30):
        return []
