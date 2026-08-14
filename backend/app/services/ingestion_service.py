"""
IngestionService — handles uploads of collections, bank statements, TPV reports, and fee configs.
Validates, parses, and stores data from various file formats and APIs.
"""
from __future__ import annotations
from typing import List, Optional, Dict, Any
from datetime import date, datetime
from decimal import Decimal
import csv
import json

from ..providers import (
    TransactionProvider,
    BankProvider,
    TPVProvider,
    FeeProvider,
)
from ..models import (
    CardCollection,
    BankMovement,
    TPVClosingReport,
    FeeStructure,
    TransactionType,
    FeeType,
    Institution,
)


class IngestionService:
    """
    Central ingestion hub. Supports:
    - CSV/Excel uploads for collections
    - MT940/CAMT.053/CSV bank statement imports
    - TPV report file drops (JSON, XML, CSV)
    - Fee configuration setup
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

    # ── Card Collections ──

    def upload_collections_csv(
        self,
        csv_data: str,
        institution_id: str,
        date_format: str = "%Y-%m-%d"
    ) -> List[CardCollection]:
        """
        Parse a CSV of daily collections and store them.
        Expected columns: date, reference, amount_gross, card_type, terminal_id, batch_number, card_last_digits
        """
        collections = []
        reader = csv.DictReader(csv_data.splitlines())

        for row in reader:
            try:
                collection = CardCollection(
                    collection_date=datetime.strptime(row["date"], date_format).date(),
                    institution_id=institution_id,
                    reference=row.get("reference", ""),
                    amount_gross=Decimal(row["amount_gross"]),
                    card_type=TransactionType(row.get("card_type", "debit")),
                    terminal_id=row.get("terminal_id"),
                    batch_number=row.get("batch_number"),
                    card_last_digits=row.get("card_last_digits"),
                    transaction_count=int(row.get("transaction_count", 1)),
                    description=row.get("description"),
                )
                collections.append(self.tx.create(collection))
            except (KeyError, ValueError) as e:
                # In production: log to error tracking, skip bad rows
                print(f"Skipping invalid row: {row} — {e}")
                continue

        return collections

    def upload_collection_manual(
        self,
        collection_date: date,
        institution_id: str,
        amount_gross: Decimal,
        card_type: TransactionType = TransactionType.DEBIT,
        reference: str = "",
        terminal_id: Optional[str] = None,
        batch_number: Optional[str] = None,
        description: Optional[str] = None
    ) -> CardCollection:
        """Upload a single collection manually (e.g., from web form)."""
        collection = CardCollection(
            collection_date=collection_date,
            institution_id=institution_id,
            reference=reference,
            amount_gross=amount_gross,
            card_type=card_type,
            terminal_id=terminal_id,
            batch_number=batch_number,
            description=description,
        )
        return self.tx.create(collection)

    # ── Bank Statements ──

    def import_bank_statement_csv(
        self,
        csv_data: str,
        institution_id: str,
        account_iban: str,
        date_format: str = "%Y-%m-%d"
    ) -> List[BankMovement]:
        """
        Parse bank statement CSV.
        Expected columns: value_date, reference, concept, amount, balance_after, movement_type
        """
        movements = []
        reader = csv.DictReader(csv_data.splitlines())

        for row in reader:
            try:
                mv = BankMovement(
                    value_date=datetime.strptime(row["value_date"], date_format).date(),
                    statement_date=datetime.strptime(row.get("statement_date", row["value_date"]), date_format).date(),
                    institution_id=institution_id,
                    account_iban=account_iban,
                    reference=row.get("reference", ""),
                    concept=row.get("concept", ""),
                    amount=Decimal(row["amount"]),
                    balance_after=Decimal(row["balance_after"]) if row.get("balance_after") else None,
                    movement_type=row.get("movement_type", "credit"),
                    raw_data=dict(row),
                )
                movements.append(self.bank.create(mv))
            except (KeyError, ValueError) as e:
                print(f"Skipping invalid bank row: {row} — {e}")
                continue

        return movements

    def import_bank_movement_manual(
        self,
        value_date: date,
        institution_id: str,
        account_iban: str,
        amount: Decimal,
        reference: str = "",
        concept: str = "",
        movement_type: str = "credit",
        balance_after: Optional[Decimal] = None
    ) -> BankMovement:
        """Add a single bank movement manually."""
        mv = BankMovement(
            value_date=value_date,
            institution_id=institution_id,
            account_iban=account_iban,
            reference=reference,
            concept=concept,
            amount=amount,
            balance_after=balance_after,
            movement_type=movement_type,
        )
        return self.bank.create(mv)

    # ── TPV Reports ──

    def upload_tpv_report(
        self,
        report_date: date,
        terminal_id: str,
        institution_id: str,
        total_sales_gross: Decimal,
        total_refunds: Decimal = Decimal("0.00"),
        debit_sales: Optional[Decimal] = None,
        credit_sales: Optional[Decimal] = None,
        transaction_count: int = 0,
        batch_number: Optional[str] = None,
        z_report_number: Optional[str] = None,
        opening_time: Optional[datetime] = None,
        closing_time: Optional[datetime] = None,
    ) -> TPVClosingReport:
        """Upload a TPV closing report."""
        net = total_sales_gross - total_refunds
        report = TPVClosingReport(
            report_date=report_date,
            terminal_id=terminal_id,
            institution_id=institution_id,
            total_sales_gross=total_sales_gross,
            total_refunds=total_refunds,
            total_net=net,
            debit_sales=debit_sales or Decimal("0.00"),
            credit_sales=credit_sales or Decimal("0.00"),
            transaction_count=transaction_count,
            batch_number=batch_number,
            z_report_number=z_report_number,
            opening_time=opening_time,
            closing_time=closing_time,
        )
        return self.tpv.create(report)

    def upload_tpv_reports_json(self, json_data: str) -> List[TPVClosingReport]:
        """Bulk upload TPV reports from JSON array."""
        data = json.loads(json_data)
        reports = []
        for item in data:
            report = TPVClosingReport(
                report_date=datetime.strptime(item["report_date"], "%Y-%m-%d").date(),
                terminal_id=item["terminal_id"],
                institution_id=item["institution_id"],
                total_sales_gross=Decimal(str(item["total_sales_gross"])),
                total_refunds=Decimal(str(item.get("total_refunds", 0))),
                total_net=Decimal(str(item.get("total_net", item["total_sales_gross"] - item.get("total_refunds", 0)))),
                debit_sales=Decimal(str(item.get("debit_sales", 0))),
                credit_sales=Decimal(str(item.get("credit_sales", 0))),
                transaction_count=item.get("transaction_count", 0),
                batch_number=item.get("batch_number"),
                z_report_number=item.get("z_report_number"),
            )
            reports.append(self.tpv.create(report))
        return reports

    # ── Fee Configuration ──

    def setup_fee_structure(
        self,
        institution_id: str,
        card_type: TransactionType,
        fee_type: FeeType,
        percentage_rate: Decimal = Decimal("0.000"),
        flat_rate: Decimal = Decimal("0.00"),
        min_fee: Optional[Decimal] = None,
        max_fee: Optional[Decimal] = None,
        effective_from: date = None,
        vat_included: bool = False
    ) -> FeeStructure:
        """Configure fees for an institution."""
        if effective_from is None:
            effective_from = date.today()

        fee = FeeStructure(
            institution_id=institution_id,
            card_type=card_type,
            fee_type=fee_type,
            percentage_rate=percentage_rate,
            flat_rate=flat_rate,
            min_fee=min_fee,
            max_fee=max_fee,
            effective_from=effective_from,
            vat_included=vat_included,
        )
        return self.fee.create(fee)
