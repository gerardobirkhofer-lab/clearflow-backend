"""Twelve months of profit. Cash stays on the other screen.

Sales come from a past month that looks like this one. The client can raise
or lower that figure. Product cost and the contract fee come off the sale.
The dated monthly bills come off last.
"""
from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.companies import companies_for_user
from app.core.auth import CurrentUser
from app.models.account_profile import AccountProfile
from app.models.expense import Expense
from app.models.product import Product
from app.models.provider import Provider
from app.models.sale_month import SaleMonth
from app.services.open_matching import madrid_today
from app.services.panel_view import _as_uuid, _money, _place_for


def _add_months(start: date, count: int) -> date:
    month = start.month - 1 + count
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def _ratio(products: list[Product]) -> float | None:
    ratios = []
    for product in products:
        price = float(product.sale_price or 0)
        if price <= 0:
            continue
        ratios.append(float(product.cost or 0) / price)
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def _base_sales(rows: list[SaleMonth], year: int, month: int) -> tuple[float | None, str | None]:
    same = [row for row in rows if row.month == month and row.year < year]
    if same:
        latest = max(same, key=lambda row: row.year)
        return _money(latest.amount), "same_month"
    if rows:
        return _money(sum(row.amount for row in rows) / len(rows)), "average"
    return None, None


async def _adjust(db: AsyncSession, tenant_id) -> float:
    row = await db.scalar(select(AccountProfile).where(AccountProfile.tenant_id == tenant_id))
    if row is None:
        return 0.0
    try:
        payload = json.loads(row.payload or "{}")
    except json.JSONDecodeError:
        return 0.0
    try:
        return float(payload.get("projection_adjust_percent") or 0)
    except (TypeError, ValueError):
        return 0.0


async def build_horizon(db: AsyncSession, current_user: CurrentUser) -> dict:
    today = madrid_today()
    companies = (await companies_for_user(db, current_user))["items"]
    places = []
    for company in companies:
        tenant_id = _as_uuid(company["id"])
        adjust = await _adjust(db, tenant_id)
        active = [site for site in company["sites"] if site.get("active", True)]
        active_ids = [str(site["id"]) for site in active]
        history = (
            await db.execute(select(SaleMonth).where(SaleMonth.tenant_id == tenant_id))
        ).scalars().all()
        products = (
            await db.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
        contracts = (
            await db.execute(
                select(Provider).where(Provider.tenant_id == tenant_id, Provider.terms_confirmed == 1)
            )
        ).scalars().all()
        fee_rates = [float(row.fee_percent or 0) / 100.0 for row in contracts if (row.fee_percent or 0) > 0]
        fee_rate = sum(fee_rates) / len(fee_rates) if fee_rates else 0.0
        expenses = (
            await db.execute(select(Expense).where(Expense.tenant_id == tenant_id))
        ).scalars().all()

        for site in active:
            site_id = str(site["id"])
            site_history = [row for row in history if str(row.site_id) == site_id]
            site_products = [
                row for row in products
                if row.site_id is None or str(row.site_id) == site_id
            ]
            cost_ratio = _ratio(site_products)
            monthly_bills = 0.0
            for expense in expenses:
                if expense.due_on is not None:
                    continue
                target = _place_for(expense.site_id, active_ids)
                if target == site_id or (target is None and len(active_ids) == 1):
                    monthly_bills += _money(expense.amount)
            months = []
            for offset in range(12):
                cursor = _add_months(today.replace(day=1), offset)
                base, sales_from = _base_sales(site_history, cursor.year, cursor.month)
                extra = 0.0
                for expense in expenses:
                    if expense.due_on is None or expense.due_on.year != cursor.year or expense.due_on.month != cursor.month:
                        continue
                    target = _place_for(expense.site_id, active_ids)
                    if target == site_id or (target is None and len(active_ids) == 1):
                        extra += _money(expense.amount)
                bills = _money(monthly_bills + extra)
                if base is None:
                    months.append({
                        "year": cursor.year,
                        "month": cursor.month,
                        "base": None,
                        "sales_from": None,
                        "sales": None,
                        "cost": None,
                        "fees": None,
                        "expenses": bills,
                        "earning": None,
                        "verdict": None,
                    })
                    continue
                sales = _money(base * (1 + adjust / 100.0))
                cost = None if cost_ratio is None else _money(sales * cost_ratio)
                fees = _money(sales * fee_rate)
                earning = None if cost is None else _money(sales - cost - fees - bills)
                months.append({
                    "year": cursor.year,
                    "month": cursor.month,
                    "base": base,
                    "sales_from": sales_from,
                    "sales": sales,
                    "cost": cost,
                    "fees": fees,
                    "expenses": bills,
                    "earning": earning,
                    "verdict": None if earning is None else ("vas_bien" if earning >= 0 else "vamos"),
                })
            places.append({
                "tenant_id": str(tenant_id),
                "site_id": site_id,
                "name": site["name"],
                "company_name": company["name"],
                "adjust_percent": adjust,
                "has_history": bool(site_history),
                "has_products": cost_ratio is not None,
                "cost_ratio": None if cost_ratio is None else round(cost_ratio, 4),
                "fee_rate": round(fee_rate, 4),
                "months": months,
            })
    return {"places": places}


async def save_adjust(db: AsyncSession, tenant_id: uuid.UUID, percent: float) -> float:
    found = await db.execute(select(AccountProfile).where(AccountProfile.tenant_id == tenant_id))
    row = found.scalar_one_or_none()
    payload = {}
    complete = 0
    if row is not None:
        complete = row.onboarding_complete
        try:
            payload = json.loads(row.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
    payload["projection_adjust_percent"] = percent
    encoded = json.dumps(payload)
    if row is None:
        row = AccountProfile(tenant_id=tenant_id, payload=encoded, onboarding_complete=0)
        db.add(row)
    else:
        row.payload = encoded
        row.onboarding_complete = complete
    await db.commit()
    return percent
