"""
FeeProvider — manages FeeStructure entities.
Calculates expected fees per transaction based on institution rules.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .base import BaseProvider
from ..models import FeeStructure, FeeType, TransactionType


class FeeProvider(BaseProvider):
    """
    Stores fee configurations and computes expected deductions.
    Supports percentage, flat, mixed, and tiered fee structures.
    """

    def __init__(self):
        self._storage: List[FeeStructure] = []
        self._index_by_institution: Dict[str, List[FeeStructure]] = {}

    def initialize(self) -> None:
        self._rebuild_indexes()

    def health_check(self) -> bool:
        return True

    def _rebuild_indexes(self) -> None:
        self._index_by_institution.clear()
        for f in self._storage:
            self._index_by_institution.setdefault(f.institution_id, []).append(f)

    # ── CRUD ──

    def create(self, fee: FeeStructure) -> FeeStructure:
        self._storage.append(fee)
        self._index_by_institution.setdefault(fee.institution_id, []).append(fee)
        return fee

    def create_many(self, fees: List[FeeStructure]) -> List[FeeStructure]:
        for f in fees:
            self.create(f)
        return fees

    def get_active_for_institution(
        self,
        institution_id: str,
        card_type: TransactionType,
        as_of: date = None
    ) -> Optional[FeeStructure]:
        """Get the currently active fee structure for an institution + card type."""
        if as_of is None:
            as_of = date.today()

        candidates = self._index_by_institution.get(institution_id, [])
        active = None
        for f in candidates:
            if not f.active:
                continue
            if f.card_type != card_type:
                continue
            if f.effective_from > as_of:
                continue
            if f.effective_until and f.effective_until < as_of:
                continue
            # Pick the most recent effective_from
            if active is None or f.effective_from > active.effective_from:
                active = f
        return active

    def calculate_fee(
        self,
        institution_id: str,
        card_type: TransactionType,
        gross_amount: Decimal,
        as_of: date = None
    ) -> Decimal:
        """
        Calculate the expected fee for a given collection.
        Returns the fee amount (positive value = cost to merchant).
        """
        fee_struct = self.get_active_for_institution(institution_id, card_type, as_of)
        if not fee_struct:
            return Decimal("0.00")

        calculated = Decimal("0.00")

        if fee_struct.fee_type == FeeType.PERCENTAGE:
            calculated = (gross_amount * fee_struct.percentage_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        elif fee_struct.fee_type == FeeType.FLAT:
            calculated = fee_struct.flat_rate

        elif fee_struct.fee_type == FeeType.MIXED:
            pct_fee = (gross_amount * fee_struct.percentage_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            calculated = max(pct_fee, fee_struct.flat_rate)

        elif fee_struct.fee_type == FeeType.TIERED and fee_struct.tier_thresholds:
            # Find applicable tier based on gross amount
            applicable_rate = Decimal("0.000")
            for threshold, rate in sorted(fee_struct.tier_thresholds.items()):
                if gross_amount >= threshold:
                    applicable_rate = rate
            calculated = (gross_amount * applicable_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        # Apply min/max constraints
        if fee_struct.min_fee is not None:
            calculated = max(calculated, fee_struct.min_fee)
        if fee_struct.max_fee is not None:
            calculated = min(calculated, fee_struct.max_fee)

        return calculated

    def get_net_after_fees(
        self,
        institution_id: str,
        card_type: TransactionType,
        gross_amount: Decimal,
        as_of: date = None
    ) -> Decimal:
        """Gross amount minus calculated fee."""
        fee = self.calculate_fee(institution_id, card_type, gross_amount, as_of)
        return gross_amount - fee

    def list_all_active(self, as_of: date = None) -> List[FeeStructure]:
        if as_of is None:
            as_of = date.today()
        results = []
        for f in self._storage:
            if not f.active:
                continue
            if f.effective_from > as_of:
                continue
            if f.effective_until and f.effective_until < as_of:
                continue
            results.append(f)
        return results
