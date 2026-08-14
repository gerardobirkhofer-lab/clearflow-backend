"""
CashFlowProvider — calculates actual balances, projected incoming settlements,
and fund availability based on reconciliation data and institution settlement delays.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from .transaction_provider import TransactionProvider
from .bank_provider import BankProvider
from .reconciliation_provider import ReconciliationProvider
from .fee_provider import FeeProvider
from ..models import CashFlowEntry, CardCollection, ReconciliationStatus, Institution


class CashFlowProvider(BaseProvider):
    """
    Builds the cash flow timeline:
    - Actual balances from bank statements
    - Projected card settlements (when pending collections will hit the bank)
    - Fee impacts
    - Available vs. pending funds
    """

    def __init__(
        self,
        tx_provider: TransactionProvider,
        bank_provider: BankProvider,
        recon_provider: ReconciliationProvider,
        fee_provider: FeeProvider,
        institutions: Optional[Dict[str, Institution]] = None
    ):
        self.tx = tx_provider
        self.bank = bank_provider
        self.recon = recon_provider
        self.fee = fee_provider
        self.institutions: Dict[str, Institution] = institutions or {}

        self._entries: List[CashFlowEntry] = []

    def initialize(self) -> None:
        pass

    def health_check(self) -> bool:
        return True

    def set_institutions(self, institutions: Dict[str, Institution]) -> None:
        self.institutions = institutions

    # ── Core Cash Flow Generation ──

    def generate_cash_flow(
        self,
        start_date: date,
        end_date: date,
        account_iban: Optional[str] = None
    ) -> List[CashFlowEntry]:
        """
        Generate a day-by-day cash flow from start_date to end_date.
        Includes actual bank movements and projected settlements.
        """
        entries: List[CashFlowEntry] = []
        running_balance = Decimal("0.00")

        # 1. Actual bank balance as of start_date
        if account_iban:
            latest_balance = self.bank.get_latest_balance(account_iban)
            if latest_balance is not None:
                running_balance = latest_balance
                entries.append(CashFlowEntry(
                    entry_date=start_date,
                    entry_type="actual_balance",
                    description=f"Opening balance ({account_iban[-4:]})",
                    amount=latest_balance,
                    is_confirmed=True,
                    running_balance=latest_balance
                ))

        current = start_date
        while current <= end_date:
            day_entries = self._build_day_entries(current, account_iban)

            for entry in day_entries:
                if entry.is_confirmed:
                    running_balance += entry.amount
                entry.running_balance = running_balance
                entries.append(entry)

            current += timedelta(days=1)

        self._entries = entries
        return entries

    def _build_day_entries(
        self,
        target_date: date,
        account_iban: Optional[str] = None
    ) -> List[CashFlowEntry]:
        """Build all cash flow entries for a single day."""
        entries: List[CashFlowEntry] = []

        # ── Actual confirmed bank movements ──
        bank_moves = self.bank.get_by_date_range(
            start=target_date,
            end=target_date,
        )
        for mv in bank_moves:
            if account_iban and mv.account_iban != account_iban:
                continue
            entries.append(CashFlowEntry(
                entry_date=target_date,
                entry_type=mv.movement_type,
                institution_id=mv.institution_id,
                description=mv.concept or f"Bank {mv.movement_type}",
                amount=mv.amount,
                is_confirmed=True,
                source_collection_id=mv.matched_collection_id,
                running_balance=None  # Will be set by caller
            ))

        # ── Projected card settlements ──
        # Find collections that should settle on this date
        pending_collections = self.tx.get_uncleared(target_date)
        for collection in pending_collections:
            institution = self.institutions.get(collection.institution_id)
            if not institution:
                continue

            expected_settlement = self._calculate_settlement_date(collection, institution)
            if expected_settlement == target_date:
                net_amount = self.fee.get_net_after_fees(
                    collection.institution_id,
                    collection.card_type,
                    collection.amount_gross,
                    collection.collection_date
                )
                entries.append(CashFlowEntry(
                    entry_date=target_date,
                    entry_type="card_settlement",
                    institution_id=collection.institution_id,
                    description=f"Projected settlement: {collection.reference or collection.id[:8]}",
                    amount=net_amount,
                    is_confirmed=False,
                    source_collection_id=collection.id,
                    expected_value_date=expected_settlement,
                    running_balance=None
                ))

        # ── Projected fees (if not already deducted in settlement) ──
        # Some institutions deduct fees separately; model as negative entries
        # This is optional based on your acquirer's behavior

        return entries

    def _calculate_settlement_date(self, collection: CardCollection, institution: Institution) -> date:
        """Calculate when a collection will settle based on institution rules."""
        if collection.expected_settlement_date:
            return collection.expected_settlement_date
        return collection.collection_date + timedelta(days=institution.settlement_delay_days)

    # ── Summary Queries ──

    def get_actual_balance(self, account_iban: str, as_of: date = None) -> Decimal:
        """Latest confirmed bank balance."""
        if as_of is None:
            as_of = date.today()
        balance = self.bank.get_latest_balance(account_iban)
        return balance if balance is not None else Decimal("0.00")

    def get_projected_incoming(
        self,
        days_ahead: int = 7,
        institution_id: Optional[str] = None
    ) -> Decimal:
        """Sum of projected settlements expected within N days."""
        today = date.today()
        total = Decimal("0.00")

        pending = self.tx.get_uncleared(today)
        for collection in pending:
            if institution_id and collection.institution_id != institution_id:
                continue

            institution = self.institutions.get(collection.institution_id)
            if not institution:
                continue

            settlement_date = self._calculate_settlement_date(collection, institution)
            if today <= settlement_date <= today + timedelta(days=days_ahead):
                net = self.fee.get_net_after_fees(
                    collection.institution_id,
                    collection.card_type,
                    collection.amount_gross,
                    collection.collection_date
                )
                total += net

        return total

    def get_pending_clearance_total(self, institution_id: Optional[str] = None) -> Decimal:
        """Total gross amount still pending bank clearance."""
        total = Decimal("0.00")
        pending = self.tx.get_uncleared(date.today(), institution_id)
        for c in pending:
            total += c.amount_gross
        return total

    def get_availability_timeline(
        self,
        collections: List[CardCollection]
    ) -> Dict[date, Decimal]:
        """
        Map of dates → amounts that will become available.
        Useful for visualizing incoming cash by day.
        """
        timeline: Dict[date, Decimal] = {}
        for collection in collections:
            institution = self.institutions.get(collection.institution_id)
            if not institution:
                continue
            settlement_date = self._calculate_settlement_date(collection, institution)
            net = self.fee.get_net_after_fees(
                collection.institution_id,
                collection.card_type,
                collection.amount_gross,
                collection.collection_date
            )
            timeline[settlement_date] = timeline.get(settlement_date, Decimal("0")) + net
        return dict(sorted(timeline.items()))

    def get_confirmed_vs_projected(self, as_of: date = None) -> Dict[str, Decimal]:
        """Summary: how much is confirmed in bank vs. still projected."""
        if as_of is None:
            as_of = date.today()

        confirmed = Decimal("0.00")
        projected = Decimal("0.00")

        for entry in self._entries:
            if entry.entry_date > as_of:
                continue
            if entry.is_confirmed:
                confirmed += entry.amount
            else:
                projected += entry.amount

        return {
            "confirmed": confirmed,
            "projected": projected,
            "total": confirmed + projected
        }
