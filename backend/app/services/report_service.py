"""Morning Report generation engine."""
from __future__ import annotations
from datetime import date, timedelta
from decimal import Decimal
from typing import List
from uuid import UUID
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from ..models_orm import MorningReport, CardCollection, BankMovement, ReconciliationResult, Institution, ReconciliationStatus
from ..schemas import InstitutionSummary, UnclearedItem, DiscrepancyItem, AlertItem

class ReportService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def generate_morning_report(self, report_date: date) -> MorningReport:
        summary = await self._get_reconciliation_summary(report_date)
        uncleared_by_inst = await self._get_uncleared_by_institution(report_date)
        top_uncleared = await self._get_top_uncleared(report_date)
        actual_balance, proj_7d, proj_30d = await self._get_cash_position(report_date)
        fees_calc, fees_disc, fee_details = await self._get_fee_summary(report_date)
        discrepancies = await self._get_recent_discrepancies(report_date)
        alerts = self._generate_alerts(summary, uncleared_by_inst, actual_balance, fees_disc)
        existing = await self.db.execute(
            select(MorningReport).where(
                and_(MorningReport.tenant_id == self.tenant_id, MorningReport.report_date == report_date)
            )
        )
        report = existing.scalar_one_or_none()
        if not report:
            report = MorningReport(tenant_id=self.tenant_id, report_date=report_date)
        report.total_collections = summary["total"]
        report.matched_count = summary["matched"]
        report.partial_count = summary["partial"]
        report.unmatched_count = summary["unmatched"]
        report.discrepancy_count = summary["discrepancy"]
        report.actual_bank_balance = actual_balance
        report.projected_incoming_7d = proj_7d
        report.projected_incoming_30d = proj_30d
        report.total_fees_yesterday = fees_calc
        report.fee_discrepancies = fee_details
        report.uncleared_by_institution = [inst.model_dump() for inst in uncleared_by_inst]
        report.top_uncleared_items = [item.model_dump() for item in top_uncleared]
        report.recent_discrepancies = [d.model_dump() for d in discrepancies]
        report.alerts = [a.model_dump() for a in alerts]
        self.db.add(report)
        await self.db.commit()
        await self.db.refresh(report)
        return report

    async def _get_reconciliation_summary(self, report_date: date) -> dict:
        stmt = select(
            func.count(ReconciliationResult.id).label("total"),
            func.count().filter(ReconciliationResult.status == ReconciliationStatus.MATCHED).label("matched"),
            func.count().filter(ReconciliationResult.status == ReconciliationStatus.PARTIAL).label("partial"),
            func.count().filter(ReconciliationResult.status == ReconciliationStatus.UNMATCHED).label("unmatched"),
            func.count().filter(ReconciliationResult.status == ReconciliationStatus.DISCREPANCY).label("discrepancy"),
        ).where(and_(ReconciliationResult.tenant_id == self.tenant_id, ReconciliationResult.collection_date == report_date))
        result = await self.db.execute(stmt)
        row = result.one()
        return {"total": row.total or 0, "matched": row.matched or 0, "partial": row.partial or 0, "unmatched": row.unmatched or 0, "discrepancy": row.discrepancy or 0}

    async def _get_uncleared_by_institution(self, report_date: date) -> List[InstitutionSummary]:
        stmt = select(Institution.id, Institution.name, func.count(CardCollection.id).label("cnt"), func.coalesce(func.sum(CardCollection.amount_gross), Decimal("0.00")).label("amt")).join(CardCollection, CardCollection.institution_id == Institution.id).where(and_(CardCollection.tenant_id == self.tenant_id, CardCollection.collection_date == report_date, CardCollection.status.in_([ReconciliationStatus.PENDING, ReconciliationStatus.PARTIAL, ReconciliationStatus.UNMATCHED, ReconciliationStatus.DISCREPANCY]))).group_by(Institution.id, Institution.name)
        result = await self.db.execute(stmt)
        rows = result.all()
        return [InstitutionSummary(id=r.id, name=r.name, uncleared_count=r.cnt, uncleared_amount=r.amt) for r in rows]

    async def _get_top_uncleared(self, report_date: date, limit: int = 20) -> List[UnclearedItem]:
        stmt = select(CardCollection.id, CardCollection.reference, CardCollection.collection_date, Institution.name.label("institution_name"), CardCollection.amount_gross).join(Institution, Institution.id == CardCollection.institution_id).where(and_(CardCollection.tenant_id == self.tenant_id, CardCollection.collection_date <= report_date, CardCollection.status.in_([ReconciliationStatus.PENDING, ReconciliationStatus.PARTIAL, ReconciliationStatus.UNMATCHED, ReconciliationStatus.DISCREPANCY]))).order_by(CardCollection.collection_date.asc()).limit(limit)
        result = await self.db.execute(stmt)
        rows = result.all()
        today = report_date
        return [UnclearedItem(collection_id=r.id, reference=r.reference, collection_date=r.collection_date, institution_name=r.institution_name, gross_amount=r.amount_gross, days_pending=(today - r.collection_date).days) for r in rows]

    async def _get_cash_position(self, report_date: date) -> tuple[Decimal, Decimal, Decimal]:
        stmt_balance = select(func.coalesce(func.sum(BankMovement.amount), Decimal("0.00"))).where(and_(BankMovement.tenant_id == self.tenant_id, BankMovement.value_date <= report_date))
        result = await self.db.execute(stmt_balance)
        actual = result.scalar_one()
        stmt_pending = select(Institution.settlement_delay_days, func.coalesce(func.sum(func.coalesce(CardCollection.amount_net, CardCollection.amount_gross)), Decimal("0.00"))).join(CardCollection, CardCollection.institution_id == Institution.id).where(and_(CardCollection.tenant_id == self.tenant_id, CardCollection.collection_date <= report_date, CardCollection.status.in_([ReconciliationStatus.PENDING, ReconciliationStatus.PARTIAL, ReconciliationStatus.UNMATCHED]))).group_by(Institution.settlement_delay_days)
        result = await self.db.execute(stmt_pending)
        rows = result.all()
        proj_7d = actual
        proj_30d = actual
        for delay, amount in rows:
            settle_date = report_date + timedelta(days=delay)
            if settle_date <= report_date + timedelta(days=7):
                proj_7d += amount
            if settle_date <= report_date + timedelta(days=30):
                proj_30d += amount
        return actual, proj_7d, proj_30d

    async def _get_fee_summary(self, report_date: date) -> tuple[Decimal, Decimal, list]:
        stmt = select(func.coalesce(func.sum(ReconciliationResult.fee_discrepancy), Decimal("0.00"))).where(and_(ReconciliationResult.tenant_id == self.tenant_id, ReconciliationResult.collection_date == report_date, ReconciliationResult.status == ReconciliationStatus.DISCREPANCY))
        result = await self.db.execute(stmt)
        fee_disc = result.scalar_one() or Decimal("0.00")
        
        stmt_details = select(
            Institution.name.label("institution_name"),
            CardCollection.reference.label("collection_reference"),
            ReconciliationResult.calculated_fee.label("expected_fee"),
            ReconciliationResult.actual_fee_deduction.label("actual_fee"),
            ReconciliationResult.fee_discrepancy.label("difference")
        ).join(CardCollection, CardCollection.id == ReconciliationResult.collection_id
        ).join(Institution, Institution.id == CardCollection.institution_id
        ).where(and_(
            ReconciliationResult.tenant_id == self.tenant_id,
            ReconciliationResult.collection_date == report_date,
            ReconciliationResult.status == ReconciliationStatus.DISCREPANCY
        ))
        result = await self.db.execute(stmt_details)
        rows = result.all()
        fee_details = [
            {
                "institution_name": r.institution_name,
                "collection_reference": r.collection_reference,
                "expected_fee": str(r.expected_fee) if r.expected_fee is not None else None,
                "actual_fee": str(r.actual_fee) if r.actual_fee is not None else None,
                "difference": str(r.difference) if r.difference is not None else "0.00"
            }
            for r in rows
        ]
        return Decimal("0.00"), fee_disc, fee_details

    async def _get_recent_discrepancies(self, report_date: date, limit: int = 20) -> List[DiscrepancyItem]:
        stmt = select(ReconciliationResult.collection_id, CardCollection.reference, Institution.name.label("institution_name"), CardCollection.amount_gross.label("expected_amount"), BankMovement.amount.label("actual_amount"), ReconciliationResult.fee_discrepancy.label("difference"), ReconciliationResult.uncleared_reason.label("reason")).join(CardCollection, CardCollection.id == ReconciliationResult.collection_id).join(Institution, Institution.id == CardCollection.institution_id).outerjoin(BankMovement, BankMovement.id == CardCollection.matched_bank_movement_id).where(and_(ReconciliationResult.tenant_id == self.tenant_id, ReconciliationResult.collection_date <= report_date, ReconciliationResult.status == ReconciliationStatus.DISCREPANCY)).order_by(ReconciliationResult.checked_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        rows = result.all()
        return [DiscrepancyItem(collection_id=r.collection_id, reference=r.reference, institution_name=r.institution_name, expected_amount=r.expected_amount, actual_amount=r.actual_amount, difference=r.difference or Decimal("0.00"), reason=r.reason) for r in rows]

    def _generate_alerts(self, summary: dict, uncleared: List[InstitutionSummary], actual_balance: Decimal, fee_discrepancy: Decimal) -> List[AlertItem]:
        alerts: List[AlertItem] = []
        if summary["unmatched"] > 0:
            alerts.append(AlertItem(severity="WARNING", message=f"{summary['unmatched']} collections remain unmatched and require attention.", category="reconciliation"))
        if summary["discrepancy"] > 0:
            alerts.append(AlertItem(severity="CRITICAL", message=f"{summary['discrepancy']} discrepancies detected in yesterday's collections.", category="discrepancy"))
        total_uncleared = sum(u.uncleared_amount for u in uncleared)
        if total_uncleared > Decimal("10000.00"):
            alerts.append(AlertItem(severity="WARNING", message=f"€{total_uncleared:,.2f} in uncleared collections across all institutions.", category="cash_flow"))
        if fee_discrepancy > Decimal("0.00"):
            alerts.append(AlertItem(severity="WARNING", message=f"Fee discrepancies totaling €{fee_discrepancy:,.2f} detected.", category="fees"))
        if actual_balance < Decimal("0.00"):
            alerts.append(AlertItem(severity="CRITICAL", message="Negative actual balance detected. Review bank movements immediately.", category="cash_flow"))
        if not alerts:
            alerts.append(AlertItem(severity="INFO", message="All systems nominal. No critical issues detected.", category="general"))
        return alerts
