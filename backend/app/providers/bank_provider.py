"""
BankProvider — manages BankMovement entities from statement imports.
Handles ingestion, matching references, and balance tracking.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from ..models import BankMovement


class BankProvider(BaseProvider):
    """
    Stores bank statement lines. In production, connect to your bank's
    API (EBA PSD2, Open Banking, SWIFT MT940, CAMT.053, etc.).
    """

    def __init__(self):
        self._storage: List[BankMovement] = []
        self._index_by_date: Dict[date, List[BankMovement]] = {}
        self._index_by_institution: Dict[str, List[BankMovement]] = {}
        self._index_by_reference: Dict[str, List[BankMovement]] = {}
        self._index_by_collection: Dict[str, BankMovement] = {}
        self._balance_by_account: Dict[str, Decimal] = {}

    def initialize(self) -> None:
        self._rebuild_indexes()

    def health_check(self) -> bool:
        return True

    def _rebuild_indexes(self) -> None:
        self._index_by_date.clear()
        self._index_by_institution.clear()
        self._index_by_reference.clear()
        self._index_by_collection.clear()
        self._balance_by_account.clear()

        for mv in self._storage:
            self._index_by_date.setdefault(mv.value_date, []).append(mv)
            self._index_by_institution.setdefault(mv.institution_id, []).append(mv)
            if mv.reference:
                self._index_by_reference.setdefault(mv.reference, []).append(mv)
            if mv.matched_collection_id:
                self._index_by_collection[mv.matched_collection_id] = mv
            if mv.account_iban and mv.balance_after is not None:
                self._balance_by_account[mv.account_iban] = mv.balance_after

    # ── CRUD ──

    def create(self, movement: BankMovement) -> BankMovement:
        self._storage.append(movement)
        self._index_by_date.setdefault(movement.value_date, []).append(movement)
        self._index_by_institution.setdefault(movement.institution_id, []).append(movement)
        if movement.reference:
            self._index_by_reference.setdefault(movement.reference, []).append(movement)
        if movement.matched_collection_id:
            self._index_by_collection[movement.matched_collection_id] = movement
        if movement.account_iban and movement.balance_after is not None:
            self._balance_by_account[movement.account_iban] = movement.balance_after
        return movement

    def create_many(self, movements: List[BankMovement]) -> List[BankMovement]:
        for m in movements:
            self.create(m)
        return movements

    def get_by_id(self, movement_id: str) -> Optional[BankMovement]:
        for mv in self._storage:
            if mv.id == movement_id:
                return mv
        return None

    def get_by_reference(self, reference: str) -> List[BankMovement]:
        return self._index_by_reference.get(reference, []).copy()

    def get_by_collection_id(self, collection_id: str) -> Optional[BankMovement]:
        return self._index_by_collection.get(collection_id)

    def get_by_date_range(
        self,
        start: date,
        end: date,
        institution_id: Optional[str] = None,
        movement_type: Optional[str] = None
    ) -> List[BankMovement]:
        results = []
        current = start
        while current <= end:
            for mv in self._index_by_date.get(current, []):
                if institution_id and mv.institution_id != institution_id:
                    continue
                if movement_type and mv.movement_type != movement_type:
                    continue
                results.append(mv)
            current += timedelta(days=1)
        return results

    def get_unmatched(self, institution_id: Optional[str] = None) -> List[BankMovement]:
        """Bank movements not yet linked to a collection (potential missing uploads)."""
        results = []
        for mv in self._storage:
            if mv.matched_collection_id is not None:
                continue
            if institution_id and mv.institution_id != institution_id:
                continue
            results.append(mv)
        return results

    def get_latest_balance(self, account_iban: str) -> Optional[Decimal]:
        """Most recent known balance for an account."""
        return self._balance_by_account.get(account_iban)

    def link_to_collection(self, movement_id: str, collection_id: str) -> Optional[BankMovement]:
        mv = self.get_by_id(movement_id)
        if not mv:
            return None
        mv.matched_collection_id = collection_id
        mv.is_reconciled = True
        self._index_by_collection[collection_id] = mv
        return mv

    def get_fees_by_date(self, fee_date: date, institution_id: Optional[str] = None) -> List[BankMovement]:
        """Extract fee/charge movements for a given date."""
        results = []
        for mv in self._index_by_date.get(fee_date, []):
            if mv.movement_type in ("fee", "charge", "commission"):
                if institution_id and mv.institution_id != institution_id:
                    continue
                results.append(mv)
        return results

    def get_net_collections_by_date(self, collection_date: date) -> Dict[str, Decimal]:
        """Sum of credit movements per institution for a date."""
        summary: Dict[str, Decimal] = {}
        for mv in self._index_by_date.get(collection_date, []):
            if mv.movement_type == "credit":
                summary[mv.institution_id] = summary.get(mv.institution_id, Decimal("0")) + mv.amount
        return summary
