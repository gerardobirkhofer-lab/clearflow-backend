"""Match only what is still open, then store that day's picture.

A matched line stays matched. An open line is tried again the next time
this runs, including the pass that repeats while the process is up.
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text

from app.core.database import SharedSessionLocal
from app.models.bank_transaction import BankTransaction
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.models.reconciliation_day import ReconciliationDay
from app.services.contract_terms import expected_net

MADRID = ZoneInfo("Europe/Madrid")


def madrid_today():
    return datetime.now(MADRID).date()


def _contract_map(rows) -> dict:
    return {(row.name or "").strip().lower(): row for row in rows if row.terms_confirmed}


def _rule_for(contracts: dict, provider_name: str | None):
    return contracts.get((provider_name or "").strip().lower())


async def match_open_items(db, tenant_id) -> int:
    """Pair open provider lines with open bank lines. Leave finished pairs alone."""
    contract_rows = (
        await db.execute(
            select(Provider).where(
                Provider.tenant_id == tenant_id,
                Provider.terms_confirmed == 1,
            )
        )
    ).scalars().all()
    contracts = _contract_map(contract_rows)
    prov_result = await db.execute(
        select(
            ProviderTransaction.id,
            ProviderTransaction.amount,
            ProviderTransaction.transaction_date,
            ProviderTransaction.reference,
            ProviderTransaction.concept,
            ProviderTransaction.provider_name,
        ).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_by_amount = defaultdict(list)
    for row in prov_result.all():
        amounts = [float(row[1])] if row[1] is not None else [0]
        rule = _rule_for(contracts, row[5])
        if rule and ((rule.fee_percent or 0) or (rule.fee_fixed or 0)):
            amounts.append(expected_net(row[1], rule.fee_percent or 0, rule.fee_fixed or 0))
        seen = set()
        for amount in amounts:
            bucket = round(amount, 0)
            for key in (bucket, bucket + 1, bucket - 1):
                if key not in seen:
                    prov_by_amount[key].append(row)
                    seen.add(key)

    total_bank = await db.scalar(
        select(func.count()).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    ) or 0

    matched_count = 0
    used_prov_ids = set()
    bank_matched_ids = []
    prov_matched_ids = []
    prov_matched_bank_ids = []
    offset = 0
    batch_size = 2000

    while offset < total_bank:
        bank_batch = (
            await db.execute(
                select(
                    BankTransaction.id,
                    BankTransaction.amount,
                    BankTransaction.transaction_date,
                    BankTransaction.reference,
                    BankTransaction.concept,
                ).where(
                    BankTransaction.tenant_id == tenant_id,
                    BankTransaction.matched == 0,
                ).offset(offset).limit(batch_size)
            )
        ).all()
        if not bank_batch:
            break
        for b_id, b_amount, b_date, b_ref, b_conc in bank_batch:
            bucket = round(float(b_amount), 0) if b_amount is not None else 0
            candidates = []
            for key in (bucket, bucket + 1, bucket - 1):
                for row in prov_by_amount.get(key, []):
                    if row[0] not in used_prov_ids:
                        candidates.append(row)
            best_match = None
            best_score = 0
            for pid, p_amount, p_date, p_ref, p_conc, p_name in candidates:
                score = 0
                agreed_hit = False
                if b_amount is not None and p_amount is not None:
                    rule = _rule_for(contracts, p_name)
                    if rule and ((rule.fee_percent or 0) or (rule.fee_fixed or 0)):
                        net = expected_net(p_amount, rule.fee_percent or 0, rule.fee_fixed or 0)
                        if abs(b_amount - p_amount) < 0.01 or abs(b_amount - net) < 0.01:
                            score += 3
                            agreed_hit = True
                    else:
                        difference = abs(b_amount - p_amount)
                        if difference < 0.01:
                            score += 3
                        elif difference < 1:
                            score += 2
                        elif difference < 5:
                            score += 1
                if agreed_hit:
                    score = max(score, 4)
                if b_date and p_date:
                    days = abs((b_date - p_date).days)
                    if days == 0:
                        score += 2
                    elif days <= 3:
                        score += 1
                if b_ref and p_ref and b_ref == p_ref:
                    score += 2
                if b_conc and p_conc and (b_conc.lower() in p_conc.lower() or p_conc.lower() in b_conc.lower()):
                    score += 1
                if score > best_score:
                    best_score = score
                    best_match = pid
            if best_match is not None and best_score >= 4:
                used_prov_ids.add(best_match)
                bank_matched_ids.append(b_id)
                prov_matched_ids.append(best_match)
                prov_matched_bank_ids.append(b_id)
                matched_count += 1
        offset += batch_size

    if bank_matched_ids:
        await db.execute(
            text("UPDATE bank_transactions SET matched = 1 WHERE id = ANY(:ids)"),
            {"ids": bank_matched_ids},
        )
        for pid, bid in zip(prov_matched_ids, prov_matched_bank_ids):
            await db.execute(
                text(
                    "UPDATE provider_transactions SET matched = 1, matched_bank_tx_id = :bid WHERE id = :pid"
                ),
                {"pid": pid, "bid": bid},
            )
    await db.commit()
    return matched_count


async def record_day(db, tenant_id, day=None) -> dict:
    """Save today's counts. An earlier day is a different row and stays as it was."""
    day = day or madrid_today()
    bank_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(case((BankTransaction.matched == 1, 1), else_=0)), 0),
                func.coalesce(func.sum(case((BankTransaction.matched == 1, BankTransaction.amount), else_=0)), 0),
                func.coalesce(func.sum(case((BankTransaction.matched == 0, 1), else_=0)), 0),
            ).where(BankTransaction.tenant_id == tenant_id)
        )
    ).one()
    provider_open = await db.scalar(
        select(func.count()).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    found = await db.execute(
        select(ReconciliationDay).where(
            ReconciliationDay.tenant_id == tenant_id,
            ReconciliationDay.day == day,
        )
    )
    row = found.scalar_one_or_none()
    if row is None:
        row = ReconciliationDay(tenant_id=tenant_id, day=day)
        db.add(row)
    row.matched_count = int(bank_row[0] or 0)
    row.matched_amount = round(float(bank_row[1] or 0), 2)
    row.open_bank_count = int(bank_row[2] or 0)
    row.open_provider_count = int(provider_open or 0)
    row.checked_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "day": day.isoformat(),
        "matched_count": row.matched_count,
        "matched_amount": row.matched_amount,
        "open_bank_count": row.open_bank_count,
        "open_provider_count": row.open_provider_count,
    }


async def watch_open_items() -> int:
    """Check every company that still has an open line."""
    async with SharedSessionLocal() as db:
        bank_ids = await db.execute(
            select(BankTransaction.tenant_id).where(BankTransaction.matched == 0).distinct()
        )
        provider_ids = await db.execute(
            select(ProviderTransaction.tenant_id).where(ProviderTransaction.matched == 0).distinct()
        )
        tenant_ids = {row[0] for row in bank_ids.all()} | {row[0] for row in provider_ids.all()}
    checked = 0
    for tenant_id in tenant_ids:
        async with SharedSessionLocal() as db:
            await match_open_items(db, tenant_id)
            await record_day(db, tenant_id)
        checked += 1
    return checked


async def watch_loop(stop: asyncio.Event) -> None:
    interval = int(os.getenv("CLEARFLOW_WATCH_SECONDS", "900"))
    while not stop.is_set():
        try:
            await watch_open_items()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
