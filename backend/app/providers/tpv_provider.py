"""
TPVProvider — manages TPVClosingReport entities.
Stores end-of-day POS terminal reports for three-way reconciliation.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date, timedelta
from decimal import Decimal

from .base import BaseProvider
from ..models import TPVClosingReport


class TPVProvider(BaseProvider):
    """Stores TPV (POS) closing reports. In production, integrate with TPV API or file drop."""

    def __init__(self):
        self._storage: List[TPVClosingReport] = []
        self._index_by_date: Dict[date, List[TPVClosingReport]] = {}
        self._index_by_terminal: Dict[str, List[TPVClosingReport]] = {}
        self._index_by_institution: Dict[str, List[TPVClosingReport]] = {}
        self._index_by_batch: Dict[str, TPVClosingReport] = {}

    def initialize(self) -> None:
        self._rebuild_indexes()

    def health_check(self) -> bool:
        return True

    def _rebuild_indexes(self) -> None:
        self._index_by_date.clear()
        self._index_by_terminal.clear()
        self._index_by_institution.clear()
        self._index_by_batch.clear()
        for r in self._storage:
            self._index_by_date.setdefault(r.report_date, []).append(r)
            self._index_by_terminal.setdefault(r.terminal_id, []).append(r)
            self._index_by_institution.setdefault(r.institution_id, []).append(r)
            if r.batch_number:
                self._index_by_batch[r.batch_number] = r

    # ── CRUD ──

    def create(self, report: TPVClosingReport) -> TPVClosingReport:
        self._storage.append(report)
        self._index_by_date.setdefault(report.report_date, []).append(report)
        self._index_by_terminal.setdefault(report.terminal_id, []).append(report)
        self._index_by_institution.setdefault(report.institution_id, []).append(report)
        if report.batch_number:
            self._index_by_batch[report.batch_number] = report
        return report

    def create_many(self, reports: List[TPVClosingReport]) -> List[TPVClosingReport]:
        for r in reports:
            self.create(r)
        return reports

    def get_by_id(self, report_id: str) -> Optional[TPVClosingReport]:
        for r in self._storage:
            if r.id == report_id:
                return r
        return None

    def get_by_batch(self, batch_number: str) -> Optional[TPVClosingReport]:
        return self._index_by_batch.get(batch_number)

    def get_by_date_range(
        self,
        start: date,
        end: date,
        institution_id: Optional[str] = None,
        terminal_id: Optional[str] = None
    ) -> List[TPVClosingReport]:
        results = []
        current = start
        while current <= end:
            for r in self._index_by_date.get(current, []):
                if institution_id and r.institution_id != institution_id:
                    continue
                if terminal_id and r.terminal_id != terminal_id:
                    continue
                results.append(r)
            current += timedelta(days=1)
        return results

    def get_by_terminal_and_date(self, terminal_id: str, report_date: date) -> Optional[TPVClosingReport]:
        for r in self._index_by_terminal.get(terminal_id, []):
            if r.report_date == report_date:
                return r
        return None

    def get_summary_by_institution(self, report_date: date) -> Dict[str, Decimal]:
        summary: Dict[str, Decimal] = {}
        for r in self._index_by_date.get(report_date, []):
            summary[r.institution_id] = summary.get(r.institution_id, Decimal("0")) + r.total_net
        return summary

    def link_to_collection(self, report_id: str, collection_id: str) -> Optional[TPVClosingReport]:
        r = self.get_by_id(report_id)
        if not r:
            return None
        r.matched_collection_id = collection_id
        return r
