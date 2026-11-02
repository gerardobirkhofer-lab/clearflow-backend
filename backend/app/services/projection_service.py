"""
Projected cash flow + P&L (projected vs actual) from live provider/bank data.

Sources of truth:
- provider_transactions (+ contract_expected_* stamps)
- providers (fee rules + credit_delay_days)
- bank_transactions for confirmed actuals when linked
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_transaction import BankTransaction
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.services.contract_math import ContractTerms, fee_on_gross

SALE_KINDS = {"sale_detail", "sale", "charge", "payment", ""}
NON_SALE_HINTS = ("fee", "payout", "refund", "transfer", "adjustment")


def _as_date(v: datetime | date | None) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _is_sale_like(tx: ProviderTransaction) -> bool:
    kind = (getattr(tx, "tx_kind", None) or "sale_detail").lower()
    if kind in SALE_KINDS or kind.startswith("sale"):
        concept = (tx.concept or "").lower()
        if any(h in concept for h in NON_SALE_HINTS) and "refund" in concept:
            return False
        return True
    return False


def _resolve_terms(
    terms_map: dict[str, ContractTerms], provider_name: str | None
) -> ContractTerms | None:
    pname = (provider_name or "").lower().strip()
    if not pname:
        return None
    if pname in terms_map:
        return terms_map[pname]
    for k, t in terms_map.items():
        if k and (k in pname or pname in k):
            return t
    return None


def _leg_economics(
    tx: ProviderTransaction, terms: ContractTerms | None
) -> dict[str, Any]:
    gross = (
        float(tx.contract_gross_basis)
        if tx.contract_gross_basis is not None
        else float(tx.gross_amount)
        if tx.gross_amount is not None
        else float(tx.amount or 0)
    )
    if tx.contract_expected_net is not None:
        fee = float(tx.contract_expected_fee or 0)
        if fee == 0 and tx.contract_expected_fee is None and gross:
            fee = round(gross - float(tx.contract_expected_net), 4)
        return {
            "gross": round(gross, 4),
            "fees": round(fee, 4),
            "net": round(float(tx.contract_expected_net), 4),
            "basis": "contract_expected_net",
        }
    if terms is not None and abs(gross) > 0:
        calc = fee_on_gross(gross, terms)
        return {
            "gross": calc["gross"],
            "fees": calc["fee_total"],
            "net": calc["net"],
            "basis": "fee_on_gross",
        }
    net = (
        float(tx.net_amount)
        if tx.net_amount is not None
        else float(tx.amount or 0)
    )
    fee = (
        float(tx.fee_amount)
        if tx.fee_amount is not None
        else round(gross - net, 4)
    )
    return {
        "gross": round(gross, 4),
        "fees": round(fee, 4),
        "net": round(net, 4),
        "basis": "gateway_reported",
    }


def _expected_deposit_date(
    sale_dt: datetime | None, terms: ContractTerms | None
) -> date | None:
    d = _as_date(sale_dt)
    if d is None:
        return None
    days = int(terms.payout_days) if terms else 2
    return d + timedelta(days=max(0, days))


async def _load_terms_map(
    db: AsyncSession, tenant_id: UUID
) -> dict[str, ContractTerms]:
    rows = (
        await db.execute(
            select(Provider).where(
                Provider.tenant_id == tenant_id,
                Provider.is_active == 1,
            )
        )
    ).scalars().all()
    out: dict[str, ContractTerms] = {}
    for p in rows:
        t = ContractTerms.from_provider(p)
        out[(p.name or "").lower()] = t
        if p.provider_type:
            out[p.provider_type.lower()] = t
    return out


async def projected_cash_flow(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    horizons: tuple[int, ...] = (7, 14, 30),
    as_of: date | None = None,
) -> dict[str, Any]:
    """
    Forward cash expected from unmatched sale legs using contract net + payout delay.
    """
    as_of = as_of or date.today()
    max_h = max(horizons)
    terms_map = await _load_terms_map(db, tenant_id)

    txs = (
        await db.execute(
            select(ProviderTransaction).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.matched == 0,
                ProviderTransaction.match_status != "confirmed",
            ).limit(8000)
        )
    ).scalars().all()

    legs: list[dict[str, Any]] = []
    skipped_wallet = 0
    skipped_non_sale = 0

    for tx in txs:
        if not _is_sale_like(tx):
            skipped_non_sale += 1
            continue
        terms = _resolve_terms(terms_map, tx.provider_name)
        if terms and terms.payout_destination == "wallet":
            skipped_wallet += 1
            continue
        eco = _leg_economics(tx, terms)
        exp_date = _expected_deposit_date(tx.transaction_date, terms)
        if exp_date is None:
            continue
        if exp_date < as_of:
            if (as_of - exp_date).days > 14:
                continue  # too old overdue
        elif exp_date > as_of + timedelta(days=max_h):
            continue
        legs.append(
            {
                "provider_tx_id": tx.id,
                "provider_name": tx.provider_name,
                "sale_date": _as_date(tx.transaction_date).isoformat()
                if tx.transaction_date
                else None,
                "expected_deposit_date": exp_date.isoformat(),
                "overdue": exp_date < as_of,
                "gross": eco["gross"],
                "fees": eco["fees"],
                "net": eco["net"],
                "amount_basis": eco["basis"],
                "match_status": tx.match_status,
                "payout_days": terms.payout_days if terms else 2,
                "reference": tx.reference,
                "settlement_id": tx.settlement_id,
            }
        )

    def pack(horizon: int) -> dict[str, Any]:
        end = as_of + timedelta(days=horizon)
        selected = [
            L
            for L in legs
            if L["overdue"]
            or (
                as_of
                <= date.fromisoformat(L["expected_deposit_date"])
                <= end
            )
        ]
        by_day: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"net": 0.0, "gross": 0.0, "count": 0}
        )
        for L in selected:
            key = L["expected_deposit_date"]
            by_day[key]["net"] = float(by_day[key]["net"]) + float(L["net"])
            by_day[key]["gross"] = float(by_day[key]["gross"]) + float(L["gross"])
            by_day[key]["count"] = int(by_day[key]["count"]) + 1
        days = [
            {
                "date": d,
                "net": round(float(v["net"]), 2),
                "gross": round(float(v["gross"]), 2),
                "count": int(v["count"]),
            }
            for d, v in sorted(by_day.items())
        ]
        return {
            "horizon_days": horizon,
            "total_net": round(sum(float(L["net"]) for L in selected), 2),
            "total_gross": round(sum(float(L["gross"]) for L in selected), 2),
            "total_fees": round(sum(float(L["fees"]) for L in selected), 2),
            "leg_count": len(selected),
            "overdue_net": round(
                sum(float(L["net"]) for L in selected if L["overdue"]), 2
            ),
            "by_day": days,
            "legs": selected[:200],
        }

    return {
        "as_of": as_of.isoformat(),
        "engine": "projection_v1_contract_settlements",
        "horizons": {str(h): pack(h) for h in horizons},
        "skipped": {
            "wallet_destination": skipped_wallet,
            "non_sale": skipped_non_sale,
        },
        "assumptions": {
            "amount_basis": "contract_expected_net then fee_on_gross then gateway",
            "delay_field": "providers.credit_delay_days",
            "includes_overdue_unmatched_up_to_14d": True,
            "excludes_wallet_payout_destination": True,
            "source": "unmatched provider_transactions (matched=0, not confirmed)",
        },
        "note": (
            "Proyección de cobros esperados en banco desde liquidaciones "
            "pendientes + reglas contractuales. Sin datos demo."
        ),
    }


async def pnl_projected_vs_actual(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """
    Period by sale date:
    - projected: contract economics on all sale-like provider txs in range
    - actual: confirmed legs (bank-linked when available)
    """
    terms_map = await _load_terms_map(db, tenant_id)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    txs = (
        await db.execute(
            select(ProviderTransaction).where(
                and_(
                    ProviderTransaction.tenant_id == tenant_id,
                    ProviderTransaction.transaction_date >= start_dt,
                    ProviderTransaction.transaction_date <= end_dt,
                )
            ).limit(10000)
        )
    ).scalars().all()

    bank_ids = [t.matched_bank_tx_id for t in txs if t.matched_bank_tx_id]
    bank_map: dict[int, BankTransaction] = {}
    if bank_ids:
        brows = (
            await db.execute(
                select(BankTransaction).where(
                    BankTransaction.tenant_id == tenant_id,
                    BankTransaction.id.in_(bank_ids),
                )
            )
        ).scalars().all()
        bank_map = {b.id: b for b in brows}

    by_provider: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        if name not in by_provider:
            by_provider[name] = {
                "name": name,
                "projected": {"gross": 0.0, "fees": 0.0, "net": 0.0, "count": 0},
                "actual": {
                    "gross": 0.0,
                    "fees": 0.0,
                    "net": 0.0,
                    "count": 0,
                    "bank_credited": 0.0,
                },
            }
        return by_provider[name]

    proj_totals = {"gross": 0.0, "fees": 0.0, "net": 0.0, "count": 0}
    act_totals = {
        "gross": 0.0,
        "fees": 0.0,
        "net": 0.0,
        "count": 0,
        "bank_credited": 0.0,
    }
    pending_count = 0

    for tx in txs:
        if not _is_sale_like(tx):
            continue
        name = (tx.provider_name or "unknown").lower()
        terms = _resolve_terms(terms_map, tx.provider_name)
        eco = _leg_economics(tx, terms)
        b = bucket(name)

        # Always count into projected for sales in period
        for dest in (proj_totals, b["projected"]):
            dest["gross"] += eco["gross"]
            dest["fees"] += eco["fees"]
            dest["net"] += eco["net"]
            dest["count"] += 1

        confirmed = tx.matched == 1 or (tx.match_status or "") == "confirmed"
        if not confirmed:
            pending_count += 1
            continue

        bank = bank_map.get(tx.matched_bank_tx_id) if tx.matched_bank_tx_id else None
        actual_net = float(bank.amount) if bank is not None else eco["net"]
        actual_gross = eco["gross"]
        actual_fees = round(actual_gross - actual_net, 4) if bank is not None else eco["fees"]

        for dest in (act_totals, b["actual"]):
            dest["gross"] += actual_gross
            dest["fees"] += actual_fees
            dest["net"] += actual_net
            dest["count"] += 1
            if bank is not None:
                dest["bank_credited"] += float(bank.amount)

    def round_block(d: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in d.items():
            out[k] = round(float(v), 2) if isinstance(v, float) else v
        return out

    providers_out = []
    for name, row in sorted(by_provider.items()):
        p = round_block(row["projected"])
        a = round_block(row["actual"])
        providers_out.append(
            {
                "name": name,
                "projected": p,
                "actual": a,
                "variance": {
                    "gross": round(a["gross"] - p["gross"], 2),
                    "fees": round(a["fees"] - p["fees"], 2),
                    "net": round(a["net"] - p["net"], 2),
                },
            }
        )

    p_tot = round_block(proj_totals)
    a_tot = round_block(act_totals)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "engine": "pnl_v1_contract_vs_confirmed",
        "projected": p_tot,
        "actual": a_tot,
        "variance": {
            "gross": round(a_tot["gross"] - p_tot["gross"], 2),
            "fees": round(a_tot["fees"] - p_tot["fees"], 2),
            "net": round(a_tot["net"] - p_tot["net"], 2),
        },
        "pending_unconfirmed_sales": pending_count,
        "by_provider": providers_out,
        "definitions": {
            "projected": (
                "Economía contractual (contract_expected_* o fee_on_gross) "
                "sobre todas las ventas del periodo (fecha de venta)."
            ),
            "actual": (
                "Solo ventas confirmadas; neto = importe del apunte bancario "
                "vinculado si existe, si no el neto contractual."
            ),
            "variance": "actual − projected (fees positivos = cobraron más fee de lo contractual)",
        },
        "note": "Sin demos. Datos de provider_transactions + contratos + banco matched.",
    }
