"""
MatchingService — orchestrates the reconciliation engine.
Can run on-demand, scheduled, or continuously.
"""
from __future__ import annotations
from typing import List, Optional
from datetime import date, timedelta

from ..providers import ReconciliationProvider
from ..models import ReconciliationResult, ReconciliationStatus


class MatchingService:
    """
    High-level service to run reconciliation.
    Wraps ReconciliationProvider with scheduling and batch logic.
    """

    def __init__(self, recon_provider: ReconciliationProvider):
        self.recon = recon_provider

    def run_daily_reconciliation(
        self,
        target_date: Optional[date] = None,
        institution_id: Optional[str] = None
    ) -> List[ReconciliationResult]:
        """
        Run reconciliation for a specific day (default: yesterday).
        This is the method your morning scheduler should call.
        """
        if target_date is None:
            target_date = date.today() - timedelta(days=1)

        return self.recon.run_reconciliation(
            target_date=target_date,
            institution_id=institution_id,
            dry_run=False
        )

    def run_backfill(
        self,
        start_date: date,
        end_date: date,
        institution_id: Optional[str] = None
    ) -> List[ReconciliationResult]:
        """Reconcile a historical date range (e.g., initial system load)."""
        all_results = []
        current = start_date
        while current <= end_date:
            results = self.recon.run_reconciliation(
                target_date=current,
                institution_id=institution_id,
                dry_run=False
            )
            all_results.extend(results)
            current += timedelta(days=1)
        return all_results

    def dry_run_reconciliation(
        self,
        target_date: date,
        institution_id: Optional[str] = None
    ) -> List[ReconciliationResult]:
        """Preview what would happen without saving changes."""
        return self.recon.run_reconciliation(
            target_date=target_date,
            institution_id=institution_id,
            dry_run=True
        )

    def resolve_discrepancy_manually(
        self,
        collection_id: str,
        resolution_status: ReconciliationStatus,
        notes: str
    ) -> Optional[ReconciliationResult]:
        """Allow manual override of a discrepancy."""
        return self.recon.resolve_manually(
            collection_id=collection_id,
            status=resolution_status,
            notes=notes
        )

    def get_pending_review(self) -> List[ReconciliationResult]:
        """All items that need human attention."""
        uncleared = self.recon.get_uncleared_results()
        discrepancies = self.recon.get_discrepancies()

        # Combine and deduplicate
        seen = set()
        results = []
        for r in discrepancies + uncleared:
            if r.collection_id not in seen:
                seen.add(r.collection_id)
                results.append(r)
        return results
