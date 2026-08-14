"""
ReconciliationProvider — the core matching engine.
Performs three-way matching: CardCollection ↔ BankMovement ↔ TPVClosingReport.
Identifies uncleared items, discrepancies, and fee mismatches.
"""
from __future__ import annotations
from typing import List, Optional, Dict, Tuple
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from .transaction_provider import TransactionProvider
from .bank_provider import BankProvider
from .tpv_provider import TPVProvider
from .fee_provider import FeeProvider
from ..models import (
    CardCollection,
    BankMovement,
    TPVClosingReport,
    ReconciliationResult,
    ReconciliationStatus,
    Institution,
)


class ReconciliationProvider(BaseProvider):
    """
    Core reconciliation engine. Runs matching logic continuously or on-demand.

    Matching strategy (in order of reliability):
    1. Exact reference match + amount match + date proximity
    2. Amount match + date proximity + institution match (fuzzy)
    3. TPV batch number → Collection batch number
    4. Manual override / pending review
    """

    def __init__(
        self,
        tx_provider: TransactionProvider,
        bank_provider: BankProvider,
        tpv_provider: TPVProvider,
        fee_provider: FeeProvider,
    ):
        self.tx = tx_provider
        self.bank = bank_provider
        self.tpv = tpv_provider
        self.fee = fee_provider

        self._results: List[ReconciliationResult] = []
        self._index_by_collection: Dict[str, ReconciliationResult] = {}
        self._index_by_date: Dict[date, List[ReconciliationResult]] = {}

    def initialize(self) -> None:
        self._rebuild_indexes()

    def health_check(self) -> bool:
        return all([
            self.tx.health_check(),
            self.bank.health_check(),
            self.tpv.health_check(),
            self.fee.health_check(),
        ])

    def _rebuild_indexes(self) -> None:
        self._index_by_collection.clear()
        self._index_by_date.clear()
        for r in self._results:
            self._index_by_collection[r.collection_id] = r
            self._index_by_date.setdefault(r.collection_date, []).append(r)

    # ── Matching Engine ──

    def run_reconciliation(
        self,
        target_date: Optional[date] = None,
        institution_id: Optional[str] = None,
        dry_run: bool = False
    ) -> List[ReconciliationResult]:
        """
        Run full reconciliation for collections on target_date.
        If target_date is None, reconciles all pending collections.
        """
        if target_date is None:
            target_date = date.today() - timedelta(days=1)

        # Get collections to reconcile
        collections = self.tx.get_by_date_range(
            start=target_date - timedelta(days=30),  # Look back 30 days for late clearings
            end=target_date,
            institution_id=institution_id,
            status=None  # All statuses, we re-evaluate
        )

        results: List[ReconciliationResult] = []

        for collection in collections:
            result = self._match_single(collection, dry_run=dry_run)
            results.append(result)

        if not dry_run:
            for r in results:
                self._store_result(r)

        return results

    def _match_single(
        self,
        collection: CardCollection,
        dry_run: bool = False
    ) -> ReconciliationResult:
        """Match one collection against bank and TPV data."""

        result = ReconciliationResult(
            collection_id=collection.id,
            collection_date=collection.collection_date,
            gross_amount=collection.amount_gross,
            status=ReconciliationStatus.PENDING,
        )

        # ── Step 1: Try to match Bank Movement ──
        bank_mv = self._find_bank_match(collection)
        if bank_mv:
            result.bank_movement_id = bank_mv.id
            result.bank_amount = bank_mv.amount
            result.amount_discrepancy = collection.amount_gross - bank_mv.amount

            # Calculate days to clear
            result.days_to_clear = (bank_mv.value_date - collection.collection_date).days

            if not dry_run:
                self.bank.link_to_collection(bank_mv.id, collection.id)

        # ── Step 2: Try to match TPV Report ──
        tpv_report = self._find_tpv_match(collection)
        if tpv_report:
            result.tpv_report_id = tpv_report.id
            result.tpv_amount = tpv_report.total_net

            if not dry_run:
                self.tpv.link_to_collection(tpv_report.id, collection.id)

        # ── Step 3: Calculate Expected Fee ──
        result.calculated_fee = self.fee.calculate_fee(
            collection.institution_id,
            collection.card_type,
            collection.amount_gross,
            collection.collection_date
        )

        # ── Step 4: Determine Status ──
        result = self._determine_status(result, collection, bank_mv, tpv_report)

        # ── Step 5: Update collection status ──
        if not dry_run:
            self.tx.update_status(
                collection.id,
                result.status,
                bank_movement_id=result.bank_movement_id,
                tpv_report_id=result.tpv_report_id,
                notes=result.uncleared_reason
            )

        return result

    def _find_bank_match(self, collection: CardCollection) -> Optional[BankMovement]:
        """Find the best matching bank movement for a collection."""

        # Strategy A: Exact reference match
        if collection.reference:
            matches = self.bank.get_by_reference(collection.reference)
            for mv in matches:
                if mv.institution_id == collection.institution_id:
                    # Verify amount is close (allow 1% tolerance for fee deduction)
                    if self._amounts_match(collection.amount_gross, mv.amount, tolerance_pct=Decimal("0.02")):
                        return mv

        # Strategy B: Amount + date + institution fuzzy match
        search_start = collection.collection_date
        search_end = collection.collection_date + timedelta(days=7)  # Clearings usually within 7 days

        candidates = self.bank.get_by_date_range(
            start=search_start,
            end=search_end,
            institution_id=collection.institution_id,
            movement_type="credit"
        )

        best_match = None
        best_score = 0

        for mv in candidates:
            if mv.matched_collection_id is not None:
                continue  # Already matched to something else

            score = 0
            # Amount match (most important)
            if self._amounts_match(collection.amount_gross, mv.amount, tolerance_pct=Decimal("0.05")):
                score += 50
            elif self._amounts_match(collection.amount_gross, mv.amount, tolerance_pct=Decimal("0.10")):
                score += 30

            # Date proximity
            day_diff = abs((mv.value_date - collection.collection_date).days)
            if day_diff == 0:
                score += 20
            elif day_diff <= 1:
                score += 15
            elif day_diff <= 3:
                score += 10

            # Reference partial match
            if collection.reference and collection.reference in mv.concept:
                score += 10
            if collection.batch_number and collection.batch_number in mv.concept:
                score += 10

            if score > best_score and score >= 60:  # Threshold for fuzzy match
                best_score = score
                best_match = mv

        return best_match

    def _find_tpv_match(self, collection: CardCollection) -> Optional[TPVClosingReport]:
        """Find matching TPV report for a collection."""

        # Strategy A: Batch number exact match
        if collection.batch_number:
            report = self.tpv.get_by_batch(collection.batch_number)
            if report and report.institution_id == collection.institution_id:
                return report

        # Strategy B: Terminal + date match
        if collection.terminal_id:
            report = self.tpv.get_by_terminal_and_date(
                collection.terminal_id,
                collection.collection_date
            )
            if report and report.institution_id == collection.institution_id:
                return report

        # Strategy C: Date + institution + amount proximity
        candidates = self.tpv.get_by_date_range(
            start=collection.collection_date,
            end=collection.collection_date,
            institution_id=collection.institution_id
        )

        for r in candidates:
            if r.matched_collection_id is not None:
                continue
            # Allow small tolerance for TPV vs collection rounding
            if self._amounts_match(collection.amount_gross, r.total_net, tolerance_pct=Decimal("0.01")):
                return r
            if self._amounts_match(collection.amount_gross, r.total_sales_gross, tolerance_pct=Decimal("0.01")):
                return r

        return None

    def _determine_status(
        self,
        result: ReconciliationResult,
        collection: CardCollection,
        bank_mv: Optional[BankMovement],
        tpv_report: Optional[TPVClosingReport]
    ) -> ReconciliationResult:
        """Determine final reconciliation status based on matches."""

        has_bank = bank_mv is not None
        has_tpv = tpv_report is not None

        if has_bank and has_tpv:
            # Fully matched — check for discrepancies
            result.status = ReconciliationStatus.MATCHED

            # Check amount discrepancy
            if result.amount_discrepancy and abs(result.amount_discrepancy) > Decimal("0.05"):
                result.status = ReconciliationStatus.DISCREPANCY
                result.uncleared_reason = (
                    f"Amount mismatch: collection={collection.amount_gross}, "
                    f"bank={bank_mv.amount}, diff={result.amount_discrepancy}"
                )

            # Check fee discrepancy (if we can infer actual fee from bank amount)
            if bank_mv.amount < collection.amount_gross:
                actual_fee = collection.amount_gross - bank_mv.amount
                result.actual_fee_deduction = actual_fee
                result.fee_discrepancy = actual_fee - result.calculated_fee
                if abs(result.fee_discrepancy) > Decimal("0.10"):
                    result.status = ReconciliationStatus.DISCREPANCY
                    result.uncleared_reason = (
                        f"Fee discrepancy: calculated={result.calculated_fee}, "
                        f"actual={actual_fee}, diff={result.fee_discrepancy}"
                    )

            # If no discrepancies, mark as cleared
            if result.status == ReconciliationStatus.MATCHED:
                result.status = ReconciliationStatus.CLEARED
                result.resolved = True

        elif has_bank and not has_tpv:
            result.status = ReconciliationStatus.PARTIAL
            result.uncleared_reason = "Matched to bank but no TPV report found"

        elif not has_bank and has_tpv:
            result.status = ReconciliationStatus.PARTIAL
            result.uncleared_reason = "Matched to TPV but not yet cleared by bank"

        else:
            # Nothing matched
            days_since = (date.today() - collection.collection_date).days
            if days_since > 7:
                result.status = ReconciliationStatus.UNMATCHED
                result.uncleared_reason = f"No match found after {days_since} days"
            else:
                result.status = ReconciliationStatus.PENDING
                result.uncleared_reason = "Awaiting bank clearing or TPV report"

        return result

    def _amounts_match(self, a: Decimal, b: Decimal, tolerance_pct: Decimal = Decimal("0.01")) -> bool:
        """Check if two amounts match within tolerance."""
        if a == Decimal("0") and b == Decimal("0"):
            return True
        if a == Decimal("0") or b == Decimal("0"):
            return False
        diff = abs(a - b)
        tolerance = max(a, b) * tolerance_pct
        return diff <= tolerance

    def _store_result(self, result: ReconciliationResult) -> None:
        """Store or update a reconciliation result."""
        existing = self._index_by_collection.get(result.collection_id)
        if existing:
            self._results.remove(existing)
        self._results.append(result)
        self._index_by_collection[result.collection_id] = result
        self._index_by_date.setdefault(result.collection_date, []).append(result)

    # ── Queries ──

    def get_result_by_collection(self, collection_id: str) -> Optional[ReconciliationResult]:
        return self._index_by_collection.get(collection_id)

    def get_uncleared_results(self, as_of: date = None) -> List[ReconciliationResult]:
        """All results that are not fully cleared."""
        if as_of is None:
            as_of = date.today()
        results = []
        for r in self._results:
            if r.status not in (ReconciliationStatus.CLEARED, ReconciliationStatus.MATCHED):
                if r.collection_date <= as_of:
                    results.append(r)
        return results

    def get_discrepancies(self) -> List[ReconciliationResult]:
        return [r for r in self._results if r.status == ReconciliationStatus.DISCREPANCY]

    def get_results_by_date(self, target_date: date) -> List[ReconciliationResult]:
        return self._index_by_date.get(target_date, []).copy()

    def resolve_manually(
        self,
        collection_id: str,
        status: ReconciliationStatus,
        notes: str = ""
    ) -> Optional[ReconciliationResult]:
        """Manual override for edge cases."""
        result = self._index_by_collection.get(collection_id)
        if not result:
            return None

        result.status = status
        result.resolved = True
        result.resolved_at = datetime.now()
        result.uncleared_reason = notes

        self.tx.update_status(
            collection_id,
            status,
            notes=notes
        )
        return result

from datetime import datetime  # noqa: E402 — imported at end to avoid circular issues in some contexts
