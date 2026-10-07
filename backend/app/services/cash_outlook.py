"""What is still due in, what arrived since the previous check, and what the month costs."""
from __future__ import annotations

from datetime import date as date_cls

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_transaction import BankTransaction
from app.models.dispute import Dispute
from app.models.expense import Expense
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.models.reconciliation_day import ReconciliationDay
from app.services.contract_terms import expected_arrival, expected_net
from app.services.open_matching import madrid_today

KINDS = {
    "rent": "Alquiler",
    "salary": "Sueldos",
    "wage": "Jornales",
    "supplier": "Proveedores",
    "tax": "Impuestos",
    "other": "Otro",
}

OPEN_DISPUTE = {"open", "escalated"}
RESOLVED_DISPUTE = {"resolved"}


def _money(value) -> float:
    return round(float(value or 0), 2)


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "date") and not isinstance(value, date_cls):
        value = value.date()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _contract_view(contracts: dict, provider_name: str | None):
    return contracts.get((provider_name or "").strip().lower())


async def build_outlook(db: AsyncSession, tenant_id) -> dict:
    today = madrid_today()
    contract_rows = (
        await db.execute(
            select(Provider).where(Provider.tenant_id == tenant_id, Provider.terms_confirmed == 1)
        )
    ).scalars().all()
    contracts = {(row.name or "").strip().lower(): row for row in contract_rows}

    provider_rows = (
        await db.execute(
            select(ProviderTransaction)
            .where(ProviderTransaction.tenant_id == tenant_id, ProviderTransaction.matched == 0)
            .order_by(ProviderTransaction.transaction_date.asc())
        )
    ).scalars().all()

    waiting = []
    waiting_amount = 0.0
    for row in provider_rows:
        rule = _contract_view(contracts, row.provider_name)
        amount = _money(row.amount)
        expected_date = None
        if rule is not None:
            amount = expected_net(row.amount, rule.fee_percent or 0, rule.fee_fixed or 0)
            arrival = expected_arrival(row.transaction_date, rule.credit_delay_days, rule.batch_day_of_week)
            expected_date = arrival.isoformat() if arrival else None
        overdue = bool(expected_date and expected_date < today.isoformat())
        waiting_amount += amount
        waiting.append({
            "concept": row.concept or row.provider_name or "Cobro",
            "amount": _money(amount),
            "expected_date": expected_date,
            "overdue": overdue,
        })
    waiting.sort(key=lambda item: (not item["overdue"], item["expected_date"] or "9999-99-99"))
    waiting_amount = _money(waiting_amount)

    bank_open_amount = await db.scalar(
        select(func.coalesce(func.sum(BankTransaction.amount), 0)).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    )
    bank_open_count = await db.scalar(
        select(func.count()).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    )
    matched_row = (
        await db.execute(
            select(
                func.count(BankTransaction.id),
                func.coalesce(func.sum(case((BankTransaction.matched == 1, BankTransaction.amount), else_=0)), 0),
            ).where(BankTransaction.tenant_id == tenant_id, BankTransaction.matched == 1)
        )
    ).one()
    matched_count = int(matched_row[0] or 0)
    matched_amount = _money(matched_row[1])

    from app.api.v1.reconciliation import account_reconciliation

    status = await account_reconciliation(db, tenant_id)
    checked = [item for item in status.get("matched") or [] if item.get("settlement")]
    fee_short_count = sum(1 for item in checked if (item["settlement"].get("fee_difference") or 0) > 0.01)
    fee_late_count = sum(1 for item in checked if (item["settlement"].get("days_late") or 0) > 0)

    day_rows = (
        await db.execute(
            select(ReconciliationDay)
            .where(ReconciliationDay.tenant_id == tenant_id)
            .order_by(ReconciliationDay.day.desc())
            .limit(14)
        )
    ).scalars().all()
    checked_on = day_rows[0].day.isoformat() if day_rows else None
    previous_day = None
    previous_open_count = None
    arrived_amount = None
    arrived_count = None
    if len(day_rows) >= 2:
        previous = day_rows[1]
        previous_day = previous.day.isoformat()
        previous_open_count = int(previous.open_bank_count or 0) + int(previous.open_provider_count or 0)
        arrived_amount = _money(day_rows[0].matched_amount - previous.matched_amount)
        arrived_count = int(day_rows[0].matched_count or 0) - int(previous.matched_count or 0)

    expense_rows = (
        await db.execute(
            select(Expense).where(Expense.tenant_id == tenant_id).order_by(Expense.id.asc())
        )
    ).scalars().all()
    expenses = [
        {
            "id": row.id,
            "kind": row.kind,
            "kind_label": KINDS.get(row.kind, row.kind),
            "concept": row.concept,
            "amount": _money(row.amount),
        }
        for row in expense_rows
    ]
    expenses_monthly = _money(sum(item["amount"] for item in expenses))

    dispute_rows = (
        await db.execute(
            select(Dispute.status, Dispute.amount, Dispute.recovery_amount).where(Dispute.tenant_id == tenant_id)
        )
    ).all()
    disputes_open_count = 0
    disputes_open_amount = 0.0
    disputes_resolved_count = 0
    disputes_resolved_amount = 0.0
    for status_name, amount, recovery in dispute_rows:
        if status_name in OPEN_DISPUTE:
            disputes_open_count += 1
            disputes_open_amount += _money(amount)
        elif status_name in RESOLVED_DISPUTE:
            disputes_resolved_count += 1
            disputes_resolved_amount += _money(recovery if recovery else amount)

    return {
        "checked_on": checked_on,
        "matched_count": matched_count,
        "matched_amount": matched_amount,
        "open_count": int(bank_open_count or 0) + len(provider_rows),
        "open_amount": _money(waiting_amount + _money(bank_open_amount)),
        "fee_checked": bool(checked),
        "fee_matches": (fee_short_count == 0 and fee_late_count == 0) if checked else None,
        "fee_short_count": fee_short_count,
        "fee_late_count": fee_late_count,
        "previous_day": previous_day,
        "previous_open_count": previous_open_count,
        "arrived_since_previous_amount": arrived_amount,
        "arrived_since_previous_count": arrived_count,
        "waiting": waiting[:12],
        "waiting_count": len(provider_rows),
        "waiting_amount": waiting_amount,
        "disputes_open_count": disputes_open_count,
        "disputes_open_amount": _money(disputes_open_amount),
        "disputes_resolved_count": disputes_resolved_count,
        "disputes_resolved_amount": _money(disputes_resolved_amount),
        "has_expenses": bool(expenses),
        "expenses_monthly": expenses_monthly,
        "left_after_expenses": _money(waiting_amount - expenses_monthly),
        "expenses": expenses,
    }
