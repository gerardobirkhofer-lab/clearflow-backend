"""
TransactionProvider — manages CardCollection entities.
Handles uploads, queries, and status updates for daily card collections.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from ..models import CardCollection, TransactionType, ReconciliationStatus


class TransactionProvider(BaseProvider):
    """
    Stores and queries card collections (debit/credit sales from TPV).
    In production, replace self._storage with a real database (PostgreSQL, etc.).
    """

    def __init__(self):
        self._storage: List[CardCollection] = []
        self._index_by_date: Dict[date, List[CardCollection]] = {}
        self._index_by_institution: Dict[str, List[CardCollection]] = {}
        self._index_by_reference: Dict[str, CardCollection] = {}
        self._index_by_status: Dict[ReconciliationStatus, List[CardCollection]] = {}

    def initialize(self) -> None:
        """Rebuild indexes from storage."""
        self._rebuild_indexes()

    def health_check(self) -> bool:
        return True

    def _rebuild_indexes(self) -> None:
        self._index_by_date.clear()
        self._index_by_institution.clear()
        self._index_by_reference.clear()
        self._index_by_status.clear()
        for tx in self._storage:
            self._index_by_date.setdefault(tx.collection_date, []).append(tx)
            self._index_by_institution.setdefault(tx.institution_id, []).append(tx)
            if tx.reference:
                self._index_by_reference[tx.reference] = tx
            self._index_by_status.setdefault(tx.status, []).append(tx)

    # ── CRUD ──

    def create(self, collection: CardCollection) -> CardCollection:
        """Store a new collection and index it."""
        self._storage.append(collection)
        self._index_by_date.setdefault(collection.collection_date, []).append(collection)
        self._index_by_institution.setdefault(collection.institution_id, []).append(collection)
        if collection.reference:
            self._index_by_reference[collection.reference] = collection
        self._index_by_status.setdefault(collection.status, []).append(collection)
        return collection

    def create_many(self, collections: List[CardCollection]) -> List[CardCollection]:
        """Bulk upload of daily collections."""
        for c in collections:
            self.create(c)
        return collections

    def get_by_id(self, collection_id: str) -> Optional[CardCollection]:
        for tx in self._storage:
            if tx.id == collection_id:
                return tx
        return None

    def get_by_reference(self, reference: str) -> Optional[CardCollection]:
        return self._index_by_reference.get(reference)

    def get_by_date_range(
        self,
        start: date,
        end: date,
        institution_id: Optional[str] = None,
        status: Optional[ReconciliationStatus] = None
    ) -> List[CardCollection]:
        """Filter collections by date range with optional institution/status."""
        results = []
        current = start
        while current <= end:
            for tx in self._index_by_date.get(current, []):
                if institution_id and tx.institution_id != institution_id:
                    continue
                if status and tx.status != status:
                    continue
                results.append(tx)
            current += timedelta(days=1)
        return results

    def get_uncleared(self, as_of: date, institution_id: Optional[str] = None) -> List[CardCollection]:
        """Collections not yet cleared by the bank (status != CLEARED)."""
        statuses = [ReconciliationStatus.PENDING, ReconciliationStatus.PARTIAL, 
                    ReconciliationStatus.UNMATCHED, ReconciliationStatus.DISCREPANCY]
        results = []
        for status in statuses:
            for tx in self._index_by_status.get(status, []):
                if tx.collection_date > as_of:
                    continue
                if institution_id and tx.institution_id != institution_id:
                    continue
                results.append(tx)
        return results

    def get_pending(self) -> List[CardCollection]:
        return self._index_by_status.get(ReconciliationStatus.PENDING, []).copy()

    def update_status(
        self,
        collection_id: str,
        status: ReconciliationStatus,
        bank_movement_id: Optional[str] = None,
        tpv_report_id: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Optional[CardCollection]:
        """Update reconciliation status and linked IDs."""
        tx = self.get_by_id(collection_id)
        if not tx:
            return None

        # Remove from old status index
        old_list = self._index_by_status.get(tx.status, [])
        if tx in old_list:
            old_list.remove(tx)

        tx.status = status
        if bank_movement_id:
            tx.matched_bank_movement_id = bank_movement_id
        if tpv_report_id:
            tx.matched_tpv_report_id = tpv_report_id
        if notes:
            tx.notes = notes

        # Add to new status index
        self._index_by_status.setdefault(status, []).append(tx)
        return tx

    def get_summary_by_institution(self, collection_date: date) -> Dict[str, Decimal]:
        """Total gross amount per institution for a given date."""
        summary: Dict[str, Decimal] = {}
        for tx in self._index_by_date.get(collection_date, []):
            summary[tx.institution_id] = summary.get(tx.institution_id, Decimal("0")) + tx.amount_gross
        return summary

    def delete(self, collection_id: str) -> bool:
        """Remove a collection (use with caution)."""
        tx = self.get_by_id(collection_id)
        if not tx:
            return False
        self._storage.remove(tx)
        self._rebuild_indexes()
        return True
