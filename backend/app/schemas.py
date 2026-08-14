"""
Pydantic v2 schemas for API requests/responses.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InstitutionSummary(BaseModel):
    id: UUID
    name: str
    uncleared_count: int
    uncleared_amount: Decimal
    model_config = ConfigDict(from_attributes=True)


class UnclearedItem(BaseModel):
    collection_id: UUID
    reference: str
    collection_date: date
    institution_name: str
    gross_amount: Decimal
    days_pending: int


class DiscrepancyItem(BaseModel):
    collection_id: UUID
    reference: str
    institution_name: str
    expected_amount: Decimal
    actual_amount: Optional[Decimal]
    difference: Decimal
    reason: Optional[str]


class AlertItem(BaseModel):
    severity: str
    message: str
    category: str


class MorningReportResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    report_date: date
    generated_at: datetime
    total_collections: int
    matched_count: int
    partial_count: int
    unmatched_count: int
    discrepancy_count: int
    actual_bank_balance: Decimal | None = None
    projected_incoming_7d: Decimal | None = None
    projected_incoming_30d: Decimal | None = None
    total_fees_yesterday: Decimal | None = None
    fee_discrepancies: list | None = None
    uncleared_by_institution: List[InstitutionSummary] | None = None
    top_uncleared_items: List[UnclearedItem] | None = None
    recent_discrepancies: List[DiscrepancyItem] | None = None
    alerts: List[AlertItem]
    model_config = ConfigDict(from_attributes=True)


class MorningReportListResponse(BaseModel):
    items: List[MorningReportResponse]
    page: int
    page_size: int


# ── Card Collection Schemas ──
class CardCollectionCreate(BaseModel):
    collection_date: date
    institution_id: UUID
    reference: str
    amount_gross: Decimal
    card_type: str = "debit"
    terminal_id: str | None = None
    batch_number: str | None = None
    card_last_digits: str | None = None
    transaction_count: int = 1
    description: str | None = None


class CardCollectionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    collection_date: date
    institution_id: UUID
    reference: str
    amount_gross: Decimal
    card_type: str
    terminal_id: str | None = None
    batch_number: str | None = None
    card_last_digits: str | None = None
    transaction_count: int
    description: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class CardCollectionListResponse(BaseModel):
    items: List[CardCollectionResponse]
    page: int
    page_size: int


class CollectionUploadResult(BaseModel):
    job_id: str
    total_rows: int
    processed: int
    failed: int
    errors: list
    collections: List[CardCollectionResponse]


# ── Reconciliation Schemas ──
class ReconciliationRunRequest(BaseModel):
    target_date: date | None = None
    institution_id: UUID | None = None
    dry_run: bool = False


class ReconciliationResultResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    collection_id: UUID
    collection_date: date
    bank_movement_id: UUID | None = None
    tpv_report_id: UUID | None = None
    status: str
    match_confidence: str | None = None
    gross_amount: Decimal
    bank_amount: Decimal | None = None
    tpv_amount: Decimal | None = None
    calculated_fee: Decimal
    actual_fee_deduction: Decimal | None = None
    fee_discrepancy: Decimal | None = None
    amount_discrepancy: Decimal | None = None
    days_to_clear: int | None = None
    uncleared_reason: str | None = None
    resolved: bool
    resolved_by_user_id: UUID | None = None
    resolved_at: datetime | None = None
    checked_at: datetime
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ReconciliationResultListResponse(BaseModel):
    items: List[ReconciliationResultResponse]
    page: int
    page_size: int


class ReconciliationRunResponse(BaseModel):
    target_date: date
    total_processed: int
    results: List[ReconciliationResultResponse]
    dry_run: bool


class ManualResolutionRequest(BaseModel):
    status: str
    notes: str | None = None


# ── Dashboard Schemas ──
class DashboardSummaryResponse(BaseModel):
    today_collections: Decimal
    yesterday_collections: Decimal
    change_percent: float
    cleared_amount: Decimal
    pending_amount: Decimal
    bank_balance: Decimal
    discrepancy_count: int
    uncleared_count: int


class CashFlowDashboardResponse(BaseModel):
    entries: list
