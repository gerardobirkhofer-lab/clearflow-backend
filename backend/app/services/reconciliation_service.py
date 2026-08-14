"""
Reconciliation Service Stub — replace with real implementation.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import List
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class ReconciliationService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def run_reconciliation(self, target_date: date, institution_id: UUID | None = None, dry_run: bool = False):
        return []

    async def get_uncleared_results(self, as_of: date, institution_id: UUID | None = None):
        return []

    async def resolve_manually(self, result_id: UUID, status: str, notes: str | None = None, resolved_by: UUID | None = None):
        from ..models_orm import ReconciliationResult
        result = await self.db.get(ReconciliationResult, result_id)
        if result and result.tenant_id == self.tenant_id:
            result.resolved = True
            result.status = status
            await self.db.commit()
            await self.db.refresh(result)
            return result
        return None
