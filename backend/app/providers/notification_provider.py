"""
NotificationProvider — generates the Morning Report.
Compiles reconciliation results, uncleared items, cash flow, and alerts
into a single daily report delivered each morning.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from .transaction_provider import TransactionProvider
from .bank_provider import BankProvider
from .reconciliation_provider import ReconciliationProvider
from .cashflow_provider import CashFlowProvider
from .fee_provider import FeeProvider
from ..models import (
    MorningReport,
    ReconciliationResult,
    ReconciliationStatus,
    CashFlowEntry,
    Institution,
)


class NotificationProvider(BaseProvider):
    """
    Generates the daily morning report with:
    - Reconciliation summary (matched / partial / unmatched / discrepancies)
    - List of uncleared collections by institution
    - Actual bank balance + projected incoming
    - Fee summary and discrepancies
    - Alerts for anomalies
    """

    def __init__(
        self,
        tx_provider: TransactionProvider,
        bank_provider: BankProvider,
        recon_provider: ReconciliationProvider,
        cashflow_provider: CashFlowProvider,
        fee_provider: FeeProvider,
        institutions: Optional[Dict[str, Institution]] = None
    ):
        self.tx = tx_provider
        self.bank = bank_provider
        self.recon = recon_provider
        self.cashflow = cashflow_provider
        self.fee = fee_provider
        self.institutions: Dict[str, Institution] = institutions or {}

    def initialize(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def set_institutions(self, institutions: Dict[str, Institution]) -> None:
        self.institutions = institutions
        self.cashflow.set_institutions(institutions)

    # ── Morning Report Generation ──

    def generate_morning_report(
        self,
        report_date: Optional[date] = None,
        account_iban: Optional[str] = None
    ) -> MorningReport:
        """
        Generate the full morning report for report_date (defaults to yesterday).
        This is the main entry point called by the scheduler each morning.
        """
        if report_date is None:
            report_date = date.today() - timedelta(days=1)

        report = MorningReport(report_date=report_date)

        # 1. Reconciliation summary for the report date
        self._build_reconciliation_summary(report, report_date)

        # 2. Uncleared collections (all pending items, not just report_date)
        self._build_uncleared_section(report, report_date)

        # 3. Cash position
        self._build_cash_position(report, account_iban)

        # 4. Fee summary
        self._build_fee_summary(report, report_date)

        # 5. Alerts
        self._build_alerts(report, report_date)

        # 6. Detail tables
        report.reconciliation_details = self.recon.get_results_by_date(report_date)
        report.cash_flow_summary = self.cashflow.generate_cash_flow(
            start_date=report_date,
            end_date=report_date + timedelta(days=14),
            account_iban=account_iban
        )

        return report

    def _build_reconciliation_summary(self, report: MorningReport, report_date: date) -> None:
        """Count statuses for collections on the report date."""
        results = self.recon.get_results_by_date(report_date)
        report.total_collections = len(results)

        for r in results:
            if r.status == ReconciliationStatus.CLEARED:
                report.matched_count += 1
            elif r.status == ReconciliationStatus.PARTIAL:
                report.partial_count += 1
            elif r.status == ReconciliationStatus.UNMATCHED:
                report.unmatched_count += 1
            elif r.status == ReconciliationStatus.DISCREPANCY:
                report.discrepancy_count += 1
            elif r.status == ReconciliationStatus.MATCHED:
                report.matched_count += 1

    def _build_uncleared_section(self, report: MorningReport, as_of: date) -> None:
        """Find all collections not yet cleared by bank or clearing institution."""
        uncleared_results = self.recon.get_uncleared_results(as_of)
        report.uncleared_collections = uncleared_results

        # Group by institution
        by_institution: Dict[str, Decimal] = {}
        for r in uncleared_results:
            collection = self.tx.get_by_id(r.collection_id)
            if not collection:
                continue
            inst_name = self.institutions.get(collection.institution_id, Institution(name="Unknown")).name
            by_institution[inst_name] = by_institution.get(inst_name, Decimal("0")) + r.gross_amount

        report.uncleared_by_institution = by_institution

    def _build_cash_position(self, report: MorningReport, account_iban: Optional[str]) -> None:
        """Current bank balance and projections."""
        if account_iban:
            report.actual_bank_balance = self.cashflow.get_actual_balance(account_iban)

        report.projected_incoming_7d = self.cashflow.get_projected_incoming(days_ahead=7)
        report.projected_incoming_30d = self.cashflow.get_projected_incoming(days_ahead=30)
        report.total_pending_clearance = self.cashflow.get_pending_clearance_total()

    def _build_fee_summary(self, report: MorningReport, report_date: date) -> None:
        """Sum of fees from yesterday and flag discrepancies."""
        fee_movements = self.bank.get_fees_by_date(report_date)
        total_fees = sum(mv.amount for mv in fee_movements)
        report.total_fees_yesterday = total_fees

        # Check for fee discrepancies in reconciliation results
        discrepancies = self.recon.get_discrepancies()
        for d in discrepancies:
            if d.fee_discrepancy and abs(d.fee_discrepancy) > Decimal("0.10"):
                collection = self.tx.get_by_id(d.collection_id)
                inst_name = "Unknown"
                if collection:
                    inst_name = self.institutions.get(collection.institution_id, Institution(name="Unknown")).name
                report.fee_discrepancies.append(
                    f"{inst_name}: Fee diff of {d.fee_discrepancy} on {d.collection_date} "
                    f"(calc={d.calculated_fee}, actual={d.actual_fee_deduction})"
                )

    def _build_alerts(self, report: MorningReport, report_date: date) -> None:
        """Generate alerts for anomalies requiring attention."""
        alerts: List[str] = []

        # Alert: Unmatched collections older than 5 days
        old_unmatched = [r for r in report.uncleared_collections 
                        if r.status == ReconciliationStatus.UNMATCHED 
                        and (report_date - r.collection_date).days > 5]
        if old_unmatched:
            total = sum(r.gross_amount for r in old_unmatched)
            alerts.append(
                f"⚠️ {len(old_unmatched)} collections unmatched for >5 days (total: {total})"
            )

        # Alert: Discrepancies
        if report.discrepancy_count > 0:
            alerts.append(
                f"⚠️ {report.discrepancy_count} discrepancies detected requiring review"
            )

        # Alert: Large pending clearance
        if report.total_pending_clearance > Decimal("10000.00"):
            alerts.append(
                f"ℹ️ Large pending clearance: {report.total_pending_clearance} across all institutions"
            )

        # Alert: Missing TPV reports for yesterday
        yesterday_collections = self.tx.get_by_date_range(report_date, report_date)
        missing_tpv = [c for c in yesterday_collections if c.matched_tpv_report_id is None]
        if missing_tpv:
            alerts.append(
                f"⚠️ {len(missing_tpv)} collections from {report_date} missing TPV report linkage"
            )

        # Alert: Bank balance dropped significantly
        if report.actual_bank_balance < Decimal("0.00"):
            alerts.append(
                f"🚨 Negative bank balance detected: {report.actual_bank_balance}"
            )

        report.alerts = alerts

    # ── Report Formatting ──

    def format_report_text(self, report: MorningReport) -> str:
        """Plain text version of the morning report for email/SMS."""
        lines = [
            f"📊 MORNING REPORT — {report.report_date}",
            f"Generated at: {report.generated_at.strftime('%Y-%m-%d %H:%M')}",
            "",
            "═" * 50,
            "RECONCILIATION SUMMARY",
            "═" * 50,
            f"  Total collections:     {report.total_collections}",
            f"  ✅ Cleared/Matched:     {report.matched_count}",
            f"  ⏳ Partial:             {report.partial_count}",
            f"  ❌ Unmatched:           {report.unmatched_count}",
            f"  ⚠️  Discrepancies:       {report.discrepancy_count}",
            "",
            "═" * 50,
            "UNCLEARED COLLECTIONS",
            "═" * 50,
        ]

        if report.uncleared_by_institution:
            for inst, amount in report.uncleared_by_institution.items():
                lines.append(f"  {inst}: {amount}")
            lines.append(f"  TOTAL PENDING: {report.total_pending_clearance}")
        else:
            lines.append("  No uncleared collections — all caught up!")

        lines.extend([
            "",
            "═" * 50,
            "CASH POSITION",
            "═" * 50,
            f"  Actual bank balance:     {report.actual_bank_balance}",
            f"  Projected incoming 7d:   {report.projected_incoming_7d}",
            f"  Projected incoming 30d:  {report.projected_incoming_30d}",
            "",
            "═" * 50,
            "FEES",
            "═" * 50,
            f"  Total fees yesterday:    {report.total_fees_yesterday}",
        ])

        if report.fee_discrepancies:
            lines.append("  Fee discrepancies:")
            for fd in report.fee_discrepancies:
                lines.append(f"    • {fd}")

        lines.extend([
            "",
            "═" * 50,
            "ALERTS",
            "═" * 50,
        ])

        if report.alerts:
            for alert in report.alerts:
                lines.append(f"  {alert}")
        else:
            lines.append("  ✅ No alerts — everything looks good.")

        lines.append("")
        lines.append("═" * 50)

        return "\n".join(lines)

    def format_report_html(self, report: MorningReport) -> str:
        """HTML version for email dashboards."""
        # Simple HTML wrapper — extend with your template system
        text = self.format_report_text(report)
        html = f"""<html><body><pre style="font-family: monospace; line-height: 1.5;">{text}</pre></body></html>"""
        return html
