"""The morning panel and the cash day, one place at a time.

Figures come from stored sales, confirmed contracts and the costs the client
typed. A sale with no place stays aside when the company has more than one.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.companies import companies_for_user
from app.core.auth import CurrentUser
from app.models.account_profile import AccountProfile
from app.models.bank_account import BankAccount
from app.models.card_operation import CardOperation
from app.models.bank_account_site import BankAccountSite
from app.models.bank_transaction import BankTransaction
from app.models.expense import Expense
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.models.reconciliation_day import ReconciliationDay
from app.services.contract_terms import expected_arrival, expected_net
from app.services.open_matching import madrid_today


def _money(value) -> float:
    return round(float(value or 0), 2)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_uuid(value):
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _day_key(value: date) -> str:
    return value.isoformat()


def split_open(carried: float, unresolved: float) -> tuple[float, float, float]:
    """carried - resolved + this check = what is still open."""
    carried = _money(carried)
    unresolved = _money(unresolved)
    if unresolved >= carried:
        return carried, 0.0, _money(unresolved - carried)
    return carried, _money(carried - unresolved), 0.0


def late_cost(amount: float, days: int, rate) -> float | None:
    if rate is None or days <= 0 or amount <= 0:
        return None
    return round(float(amount) * float(rate) / 100.0 * int(days) / 365.0, 2)


def _bill_on(expense: Expense, day: date) -> bool:
    if expense.due_on is not None:
        return expense.due_on == day
    if not expense.due_day:
        return False
    last = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    due = min(int(expense.due_day), last.day)
    return day.day == due


def _place_for(site_id, active_ids: list) -> str | None:
    key = str(site_id) if site_id else None
    if key and key in active_ids:
        return key
    if len(active_ids) == 1:
        return active_ids[0]
    return None


async def _rates(db: AsyncSession, tenant_ids: list) -> dict:
    if not tenant_ids:
        return {}
    rows = (
        await db.execute(select(AccountProfile).where(AccountProfile.tenant_id.in_(tenant_ids)))
    ).scalars().all()
    found = {}
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        raw = payload.get("debt_rate_percent")
        try:
            found[str(row.tenant_id)] = float(raw) if raw is not None and raw != "" else None
        except (TypeError, ValueError):
            found[str(row.tenant_id)] = None
    return found


async def _previous_open(db: AsyncSession, tenant_id, today: date) -> dict:
    row = (
        await db.execute(
            select(ReconciliationDay)
            .where(ReconciliationDay.tenant_id == tenant_id, ReconciliationDay.day < today)
            .order_by(ReconciliationDay.day.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not row.place_open:
        return {}
    try:
        data = json.loads(row.place_open)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _remember_open(db: AsyncSession, tenant_id, today: date, places: dict) -> datetime:
    found = await db.execute(
        select(ReconciliationDay).where(
            ReconciliationDay.tenant_id == tenant_id,
            ReconciliationDay.day == today,
        )
    )
    row = found.scalar_one_or_none()
    if row is None:
        row = ReconciliationDay(tenant_id=tenant_id, day=today)
        db.add(row)
    row.place_open = json.dumps(places)
    row.checked_at = datetime.now(timezone.utc)
    return row.checked_at


def _when_day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def claim_lines(sales, operations, contracts) -> dict[str, list]:
    """Name the ticket. A short fee goes to the acquirer. A sale with no card charge stays in the place."""
    used_sales = set()
    used_ops = set()
    by_ref = {}
    for sale in sales:
        if sale.reference:
            by_ref.setdefault(str(sale.reference), []).append(sale)
    pairs = []
    for operation in operations:
        partner = None
        if operation.reference and by_ref.get(str(operation.reference)):
            for sale in by_ref[str(operation.reference)]:
                if id(sale) not in used_sales:
                    partner = sale
                    break
        if partner is None:
            op_day = _when_day(operation.operated_at)
            for sale in sales:
                if id(sale) in used_sales:
                    continue
                if str(sale.site_id or "") != str(operation.site_id):
                    continue
                if _when_day(sale.transaction_date) != op_day:
                    continue
                if abs(_money(sale.amount) - _money(operation.amount)) > 0.01:
                    continue
                partner = sale
                break
        if partner is not None:
            used_sales.add(id(partner))
            used_ops.add(id(operation))
            pairs.append((partner, operation))

    lines: dict[str, list] = {}
    covered = {(str(operation.site_id), _when_day(operation.operated_at)) for operation in operations}
    for sale, operation in pairs:
        rule = contracts.get((sale.provider_name or "").strip().lower())
        if rule is None or operation.fee_amount is None:
            continue
        expected_fee = _money(sale.amount) - expected_net(_money(sale.amount), rule.fee_percent or 0, rule.fee_fixed or 0)
        short = round(_money(operation.fee_amount) - expected_fee, 2)
        if short <= 0.01:
            continue
        key = str(sale.site_id)
        lines.setdefault(key, []).append({
            "kind": "liquidador",
            "concept": sale.concept or sale.provider_name,
            "amount": _money(sale.amount),
            "sold_on": _when_day(sale.transaction_date).isoformat() if _when_day(sale.transaction_date) else None,
            "auth_code": operation.auth_code,
            "short": short,
            "detail": "La comisión de esta operación es mayor que el contrato.",
        })
    for sale in sales:
        if id(sale) in used_sales:
            continue
        day = _when_day(sale.transaction_date)
        key = str(sale.site_id or "")
        if (key, day) not in covered:
            continue
        lines.setdefault(key, []).append({
            "kind": "caja",
            "concept": sale.concept or sale.provider_name,
            "amount": _money(sale.amount),
            "sold_on": day.isoformat() if day else None,
            "auth_code": None,
            "short": _money(sale.amount),
            "detail": "Este ticket no tiene una operación de tarjeta.",
        })
    return lines


def _empty_place(site, company) -> dict:
    return {
        "site_id": str(site["id"]) if site else None,
        "name": site["name"] if site else "Cobros sin local",
        "company_id": str(company["id"]),
        "company_name": company["name"],
        "active": True if site is None else bool(site.get("active", True)),
        "sales": 0.0,
        "collected": 0.0,
        "carried_open": 0.0,
        "resolved": 0.0,
        "this_check": 0.0,
        "unresolved": 0.0,
        "fees": 0.0,
        "late_amount": 0.0,
        "late_days": 0,
        "late_cost": None,
        "debt_rate": None,
        "uncollected_amount": 0.0,
        "uncollected_count": 0,
        "contract_fees": 0.0,
        "expenses": 0.0,
        "earning": 0.0,
        "verdict": "vas_bien",
        "claims": [],
    }


async def build_panel(db: AsyncSession, current_user: CurrentUser) -> dict:
    today = madrid_today()
    companies = (await companies_for_user(db, current_user))["items"]
    tenant_ids = [_as_uuid(item["id"]) for item in companies]
    rates = await _rates(db, tenant_ids)
    checked_at = None
    places = []

    for company in companies:
        tenant_id = _as_uuid(company["id"])
        active = [site for site in company["sites"] if site.get("active", True)]
        active_ids = [str(site["id"]) for site in active]
        buckets = {site_id: _empty_place(next(site for site in active if str(site["id"]) == site_id), company) for site_id in active_ids}
        loose = _empty_place(None, company)
        rate = rates.get(str(tenant_id))
        for bucket in list(buckets.values()) + [loose]:
            bucket["debt_rate"] = rate

        contracts = {
            (row.name or "").strip().lower(): row
            for row in (
                await db.execute(
                    select(Provider).where(Provider.tenant_id == tenant_id, Provider.terms_confirmed == 1)
                )
            ).scalars().all()
        }
        sales = (
            await db.execute(select(ProviderTransaction).where(ProviderTransaction.tenant_id == tenant_id))
        ).scalars().all()
        banks = {
            row.id: row
            for row in (
                await db.execute(select(BankTransaction).where(BankTransaction.tenant_id == tenant_id))
            ).scalars().all()
        }
        for sale in sales:
            bucket = buckets.get(_place_for(sale.site_id, active_ids)) or loose
            amount = _money(sale.amount)
            bucket["sales"] = _money(bucket["sales"] + amount)
            rule = contracts.get((sale.provider_name or "").strip().lower())
            if rule is not None:
                bucket["contract_fees"] = _money(
                    bucket["contract_fees"] + amount - expected_net(amount, rule.fee_percent or 0, rule.fee_fixed or 0)
                )
            if int(sale.matched or 0) == 1:
                bucket["collected"] = _money(bucket["collected"] + amount)
                bank = banks.get(sale.matched_bank_tx_id)
                if rule is not None and bank is not None:
                    net = expected_net(amount, rule.fee_percent or 0, rule.fee_fixed or 0)
                    short = round(net - _money(bank.amount), 2)
                    if short > 0.01:
                        bucket["fees"] = _money(bucket["fees"] + short)
                    arrival = expected_arrival(sale.transaction_date, rule.credit_delay_days, rule.batch_day_of_week)
                    bank_day = bank.transaction_date.date() if isinstance(bank.transaction_date, datetime) else bank.transaction_date
                    if arrival and bank_day and bank_day > arrival:
                        days = (bank_day - arrival).days
                        bucket["late_amount"] = _money(bucket["late_amount"] + _money(bank.amount))
                        bucket["late_days"] = max(bucket["late_days"], days)
            else:
                bucket["unresolved"] = _money(bucket["unresolved"] + amount)
                bucket["uncollected_amount"] = _money(bucket["uncollected_amount"] + amount)
                bucket["uncollected_count"] += 1

        operations = (
            await db.execute(select(CardOperation).where(CardOperation.tenant_id == tenant_id))
        ).scalars().all()
        for site_key, lines in claim_lines(sales, operations, contracts).items():
            if site_key in buckets:
                buckets[site_key]["claims"] = lines

        previous = await _previous_open(db, tenant_id, today)
        remembered = {}
        for key, bucket in list(buckets.items()) + [("unassigned", loose)]:
            carried, resolved, opened = split_open(previous.get(key) or 0, bucket["unresolved"])
            bucket["carried_open"] = carried
            bucket["resolved"] = resolved
            bucket["this_check"] = opened
            bucket["late_cost"] = late_cost(bucket["late_amount"], bucket["late_days"], rate)
            remembered[key] = bucket["unresolved"]
        checked_at = await _remember_open(db, tenant_id, today, remembered)

        expenses = (
            await db.execute(select(Expense).where(Expense.tenant_id == tenant_id))
        ).scalars().all()
        for expense in expenses:
            amount = _money(expense.amount)
            target = _place_for(expense.site_id, active_ids)
            if target:
                buckets[target]["expenses"] = _money(buckets[target]["expenses"] + amount)
            elif len(active_ids) == 1 and active_ids:
                buckets[active_ids[0]]["expenses"] = _money(buckets[active_ids[0]]["expenses"] + amount)

        for bucket in buckets.values():
            bucket["earning"] = _money(bucket["sales"] - bucket["contract_fees"] - bucket["expenses"])
            bucket["verdict"] = "vas_bien" if bucket["earning"] >= 0 else "vamos"
            places.append(bucket)
        if any(loose[key] for key in ("sales", "collected", "unresolved", "fees", "late_amount")):
            loose["earning"] = _money(loose["sales"] - loose["contract_fees"])
            loose["verdict"] = "vas_bien" if loose["earning"] >= 0 else "vamos"
            places.append(loose)

    await db.commit()
    unresolved_total = _money(sum(item["unresolved"] for item in places))
    return {
        "checked_at": checked_at.isoformat() if checked_at else datetime.now(timezone.utc).isoformat(),
        "unresolved_total": unresolved_total,
        "places": places,
        "horizon": "soon",
    }


def month_days(today: date) -> list[date]:
    """Every day of the month that contains today."""
    start = today.replace(day=1)
    if start.month == 12:
        nxt = date(start.year + 1, 1, 1)
    else:
        nxt = date(start.year, start.month + 1, 1)
    last = nxt - timedelta(days=1)
    days = []
    cursor = start
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


async def build_caja(db: AsyncSession, current_user: CurrentUser) -> dict:
    panel = await build_panel(db, current_user)
    today = madrid_today()
    days = month_days(today)
    companies = (await companies_for_user(db, current_user))["items"]
    by_site = {item["site_id"]: item for item in panel["places"] if item["site_id"]}
    result = []
    for company in companies:
        tenant_id = _as_uuid(company["id"])
        active = [site for site in company["sites"] if site.get("active", True)]
        active_ids = [str(site["id"]) for site in active]
        contracts = {
            (row.name or "").strip().lower(): row
            for row in (
                await db.execute(
                    select(Provider).where(Provider.tenant_id == tenant_id, Provider.terms_confirmed == 1)
                )
            ).scalars().all()
        }
        sales = (
            await db.execute(
                select(ProviderTransaction).where(
                    ProviderTransaction.tenant_id == tenant_id,
                    ProviderTransaction.matched == 0,
                )
            )
        ).scalars().all()
        expenses = (
            await db.execute(select(Expense).where(Expense.tenant_id == tenant_id))
        ).scalars().all()
        accounts = (
            await db.execute(select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.is_active == 1))
        ).scalars().all()
        links = (
            await db.execute(
                select(BankAccountSite).where(BankAccountSite.bank_account_id.in_([row.id for row in accounts] or [-1]))
            )
        ).scalars().all()
        linked: dict[int, set[str]] = {}
        for link in links:
            linked.setdefault(link.bank_account_id, set()).add(str(link.site_id))
        latest_balance = (
            await db.execute(
                select(BankTransaction.balance)
                .where(BankTransaction.tenant_id == tenant_id, BankTransaction.balance.isnot(None))
                .order_by(BankTransaction.transaction_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        for site in active:
            site_id = str(site["id"])
            place = by_site.get(site_id) or _empty_place(site, company)
            exclusive = [
                account for account in accounts
                if linked.get(account.id) == {site_id} or (len(active_ids) == 1 and not linked.get(account.id))
            ]
            opening = None
            typed = [account.opening_balance for account in exclusive if account.opening_balance is not None]
            if typed:
                opening = _money(sum(typed))
            elif len(active_ids) == 1 and latest_balance is not None:
                opening = _money(latest_balance)

            inflows = { _day_key(day): 0.0 for day in days }
            for sale in sales:
                if _place_for(sale.site_id, active_ids) != site_id:
                    continue
                rule = contracts.get((sale.provider_name or "").strip().lower())
                if rule is None:
                    continue
                arrival = expected_arrival(sale.transaction_date, rule.credit_delay_days, rule.batch_day_of_week)
                key = _iso(arrival)
                if key in inflows:
                    inflows[key] = _money(inflows[key] + expected_net(sale.amount, rule.fee_percent or 0, rule.fee_fixed or 0))

            strip = []
            running = opening
            first_gap = None
            for day in days:
                key = _day_key(day)
                due = []
                for expense in expenses:
                    target = _place_for(expense.site_id, active_ids)
                    belongs = target == site_id or (target is None and len(active_ids) == 1)
                    if belongs and _bill_on(expense, day):
                        due.append({"concept": expense.concept, "amount": _money(expense.amount)})
                outflow = _money(sum(item["amount"] for item in due))
                arrived = inflows[key]
                closing = None if running is None else _money(running + arrived - outflow)
                covers = None if closing is None else closing >= 0
                if first_gap is None and day >= today and covers is False:
                    first_gap = {"date": key, "bills": due, "closing": closing}
                strip.append({
                    "date": key,
                    "is_today": day == today,
                    "opening": running,
                    "inflows": arrived,
                    "outflows": outflow,
                    "closing": closing,
                    "bills": due,
                    "covers": covers,
                })
                running = closing
            upcoming = [
                {"date": row["date"], "bills": row["bills"]}
                for row in strip
                if row["date"] >= today.isoformat() and row["bills"]
            ]
            result.append({
                **place,
                "opening_known": opening is not None,
                "month": today.strftime("%Y-%m"),
                "first_gap": first_gap,
                "upcoming": upcoming,
                "days": strip,
            })
    return {"checked_at": panel["checked_at"], "places": result}
