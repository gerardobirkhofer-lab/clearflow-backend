"""
Core data models for the Reconciliation & Cash Flow System.
All entities use UUID primary keys and explicit typing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import Optional, List, Dict


class TransactionType(Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class ReconciliationStatus(Enum):
    PENDING = auto()           # Uploaded but not yet processed
    MATCHED = auto()           # Fully reconciled across all sources
    PARTIAL = auto()           # Matched some sources, missing others
    UNMATCHED = auto()         # No match found
    DISCREPANCY = auto()       # Matched but amount/date differs
    CLEARED = auto()           # Bank confirmed, TPV confirmed, fees applied


class FeeType(Enum):
    PERCENTAGE = "percentage"  # e.g., 1.5% of transaction
    FLAT = "flat"              # e.g., €0.10 per transaction
    MIXED = "mixed"            # percentage + flat minimum
    TIERED = "tiered"          # different rates per volume band


@dataclass
class Institution:
    """Bank, acquirer, or clearing institution."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    code: str = ""                    # Internal code (e.g., "SANTANDER", "RED_SYS")
    institution_type: str = ""        # "bank", "acquirer", "clearing_house"
    settlement_delay_days: int = 1    # D+1, D+2, etc.
    contact_email: Optional[str] = None
    active: bool = True
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class FeeStructure:
    """Fee configuration per institution and card type."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    institution_id: str = ""
    card_type: TransactionType = TransactionType.DEBIT
    fee_type: FeeType = FeeType.PERCENTAGE
    percentage_rate: Decimal = Decimal("0.000")      # e.g., 0.015 for 1.5%
    flat_rate: Decimal = Decimal("0.00")             # e.g., 0.10
    min_fee: Optional[Decimal] = None                # minimum fee per tx
    max_fee: Optional[Decimal] = None                # maximum fee per tx
    tier_thresholds: Optional[Dict[Decimal, Decimal]] = None  # {volume: rate}
    vat_included: bool = False
    effective_from: date = field(default_factory=date.today)
    effective_until: Optional[date] = None
    active: bool = True


@dataclass
class CardCollection:
    """Daily collection uploaded: sales paid by card in your TPV."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    collection_date: date = field(default_factory=date.today)
    upload_timestamp: datetime = field(default_factory=datetime.now)
    institution_id: str = ""
    card_type: TransactionType = TransactionType.DEBIT
    terminal_id: Optional[str] = None
    batch_number: Optional[str] = None
    reference: str = ""               # Unique reference for matching (e.g., auth code + date)
    amount_gross: Decimal = Decimal("0.00")
    amount_net: Optional[Decimal] = None   # After fees, if known at upload
    card_last_digits: Optional[str] = None
    transaction_count: int = 1
    description: Optional[str] = None
    status: ReconciliationStatus = ReconciliationStatus.PENDING
    matched_bank_movement_id: Optional[str] = None
    matched_tpv_report_id: Optional[str] = None
    expected_settlement_date: Optional[date] = None
    notes: Optional[str] = None


@dataclass
class BankMovement:
    """Line from a bank statement import."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    statement_date: date = field(default_factory=date.today)
    value_date: date = field(default_factory=date.today)
    booking_date: datetime = field(default_factory=datetime.now)
    institution_id: str = ""
    account_iban: Optional[str] = None
    movement_type: str = ""           # "credit", "debit", "fee", "chargeback"
    reference: str = ""               # For matching against collections
    concept: str = ""                 # Bank description
    amount: Decimal = Decimal("0.00")
    balance_after: Optional[Decimal] = None
    currency: str = "EUR"
    matched_collection_id: Optional[str] = None
    matched_fee_id: Optional[str] = None
    is_reconciled: bool = False
    raw_data: Optional[Dict] = None   # Original import row for traceability


@dataclass
class TPVClosingReport:
    """End-of-day report from the POS terminal / TPV system."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    report_date: date = field(default_factory=date.today)
    terminal_id: str = ""
    institution_id: str = ""
    opening_time: Optional[datetime] = None
    closing_time: Optional[datetime] = None
    total_sales_gross: Decimal = Decimal("0.00")
    total_refunds: Decimal = Decimal("0.00")
    total_net: Decimal = Decimal("0.00")
    transaction_count: int = 0
    debit_sales: Decimal = Decimal("0.00")
    credit_sales: Decimal = Decimal("0.00")
    batch_number: Optional[str] = None
    z_report_number: Optional[str] = None
    matched_collection_id: Optional[str] = None
    discrepancies: Optional[str] = None


@dataclass
class ReconciliationResult:
    """Output of the matching engine for a single collection."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    collection_id: str = ""
    collection_date: date = field(default_factory=date.today)
    bank_movement_id: Optional[str] = None
    tpv_report_id: Optional[str] = None
    status: ReconciliationStatus = ReconciliationStatus.PENDING
    gross_amount: Decimal = Decimal("0.00")
    bank_amount: Optional[Decimal] = None
    tpv_amount: Optional[Decimal] = None
    calculated_fee: Decimal = Decimal("0.00")
    actual_fee_deduction: Optional[Decimal] = None
    fee_discrepancy: Optional[Decimal] = None
    amount_discrepancy: Optional[Decimal] = None
    days_to_clear: Optional[int] = None   # How many days until bank clearing
    uncleared_reason: Optional[str] = None
    resolved: bool = False
    checked_at: datetime = field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None


@dataclass
class CashFlowEntry:
    """A single line in the cash flow forecast."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    entry_date: date = field(default_factory=date.today)
    entry_type: str = ""              # "actual_balance", "card_settlement", "fee", "other"
    institution_id: Optional[str] = None
    description: str = ""
    amount: Decimal = Decimal("0.00")
    is_confirmed: bool = False        # True if already in bank, False if projected
    source_collection_id: Optional[str] = None
    expected_value_date: Optional[date] = None
    running_balance: Optional[Decimal] = None


@dataclass
class MorningReport:
    """The daily report generated each morning."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    report_date: date = field(default_factory=date.today)
    generated_at: datetime = field(default_factory=datetime.now)

    # Reconciliation summary
    total_collections: int = 0
    matched_count: int = 0
    partial_count: int = 0
    unmatched_count: int = 0
    discrepancy_count: int = 0

    # Uncleared items
    uncleared_collections: List[ReconciliationResult] = field(default_factory=list)
    uncleared_by_institution: Dict[str, Decimal] = field(default_factory=dict)

    # Cash position
    actual_bank_balance: Decimal = Decimal("0.00")
    projected_incoming_7d: Decimal = Decimal("0.00")
    projected_incoming_30d: Decimal = Decimal("0.00")
    total_pending_clearance: Decimal = Decimal("0.00")

    # Fee summary
    total_fees_yesterday: Decimal = Decimal("0.00")
    fee_discrepancies: List[str] = field(default_factory=list)

    # Alerts
    alerts: List[str] = field(default_factory=list)

    # Detail tables
    reconciliation_details: List[ReconciliationResult] = field(default_factory=list)
    cash_flow_summary: List[CashFlowEntry] = field(default_factory=list)
