"""
ClearFlow Control Orchestrator — one control run, two strategies.

  Detail:      use liquidator/bank line detail (existing matching engine)
  Reconstruct: build expected net/date from contract + sales, check bank/wallet

Always documented in match_evidence / disputes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_transaction import BankTransaction
from app.models.dispute import Dispute
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.services.contract_math import ContractTerms, expected_from_gross, gross_up_from_net

CONTROL_ENGINE = "clearflow_control_v1"
AMOUNT_tol = 0.05  # € — reconstruct tolerance (slightly looser than detail 0.01)


async def run_control(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    run_detail: bool = True,
    run_reconstruct: bool = True,
    create_disputes: bool = True,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "engine": CONTROL_ENGINE,
        "doctrine": (
            "Banco + wallet + facturación + contrato. "
            "Con detalle → Strategy Detail; sin detalle → Strategy Reconstruct. "
            "Siempre documentado."
        ),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "detail": None,
        "reconstruct": None,
    }

    if run_detail:
        from app.api.v1.reconciliation import execute_reconciliation

        out["detail"] = await execute_reconciliation(
            db, tenant_id, date_from=date_from, date_to=date_to
        )

    if run_reconstruct:
        out["reconstruct"] = await _run_reconstruct(
            db,
            tenant_id,
            create_disputes=create_disputes,
            date_from=date_from,
            date_to=date_to,
        )

    return out


async def _run_reconstruct(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    create_disputes: bool,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    providers = (
        await db.execute(
            select(Provider).where(
                Provider.tenant_id == tenant_id,
                Provider.is_active == 1,
            )
        )
    ).scalars().all()
    terms_by_name: dict[str, ContractTerms] = {}
    for p in providers:
        t = ContractTerms.from_provider(p)
        terms_by_name[t.provider_name.lower()] = t
        # also key by provider_type aliases
        pt = (p.provider_type or "").lower()
        if pt:
            terms_by_name[pt] = t

    if not terms_by_name:
        return {
            "status": "skipped",
            "reason": "no_contract_terms_configured",
            "hint": "Cargá fees y D+X del contrato en Setup / providers.",
        }

    sale_filters = [
        ProviderTransaction.tenant_id == tenant_id,
        ProviderTransaction.matched == 0,
        or_(
            ProviderTransaction.match_status == "pending",
            ProviderTransaction.match_status.is_(None),
            ProviderTransaction.match_status == "suggested",
        ),
    ]
    if date_from is not None:
        sale_filters.append(ProviderTransaction.transaction_date >= date_from)
    if date_to is not None:
        sale_filters.append(ProviderTransaction.transaction_date <= date_to)

    # Unmatched provider legs = sales without detail-match
    sales = (
        await db.execute(
            select(ProviderTransaction).where(*sale_filters).limit(3000)
        )
    ).scalars().all()

    bank_filters = [
        BankTransaction.tenant_id == tenant_id,
        BankTransaction.matched == 0,
    ]
    if date_from is not None:
        bank_filters.append(BankTransaction.transaction_date >= date_from)
    if date_to is not None:
        bank_filters.append(BankTransaction.transaction_date <= date_to)

    unmatched_banks = (
        await db.execute(select(BankTransaction).where(*bank_filters))
    ).scalars().all()
    # Index bank by rounded amount for candidate lookup
    banks_by_amt: dict[float, list[BankTransaction]] = {}
    for b in unmatched_banks:
        if b.amount is None:
            continue
        key = round(float(b.amount), 2)
        banks_by_amt.setdefault(key, []).append(b)

    used_bank_ids: set[int] = set()
    confirmed = 0
    disputed = 0
    wallet_pending = 0
    samples: list[dict] = []
    now = datetime.utcnow()

    for sale in sales:
        pname = (sale.provider_name or "").lower()
        terms = terms_by_name.get(pname)
        if not terms:
            # try fuzzy contains
            for k, t in terms_by_name.items():
                if k and (k in pname or pname in k):
                    terms = t
                    break
        if not terms:
            continue

        gross = float(
            sale.gross_amount
            if sale.gross_amount is not None
            else (sale.amount or 0)
        )
        if abs(gross) < 0.01:
            continue

        expected = expected_from_gross(gross, sale.transaction_date, terms)
        expected_net = float(expected["net"])
        dest = terms.payout_destination

        if dest == "wallet":
            # Document expectation; cannot verify bank credit
            evidence = {
                "engine": CONTROL_ENGINE,
                "strategy": "reconstruct",
                "decision": "wallet_expected_not_bank",
                "expected": expected,
                "sale_provider_tx_id": sale.id,
                "note": (
                    "Contrato indica destino wallet. "
                    "Sin ledger de liquidadora no se auto-confirma abono banco."
                ),
            }
            sale.match_status = "pending_wallet"
            sale.match_evidence = json.dumps(evidence, ensure_ascii=False)
            wallet_pending += 1
            if len(samples) < 15:
                samples.append(evidence)
            continue

        # Find bank credit ≈ expected net, within payout window (+2d grace)
        bank_hit: BankTransaction | None = None
        target = round(expected_net, 2)
        candidates = list(banks_by_amt.get(target, []))
        # also nearby amounts within tolerance
        for k, lst in banks_by_amt.items():
            if abs(k - target) <= amount_tol and k != target:
                candidates.extend(lst)

        window_end = None
        window_start = None
        if sale.transaction_date:
            window_start = sale.transaction_date - timedelta(days=1)
            window_end = sale.transaction_date + timedelta(
                days=max(0, terms.payout_days) + 2
            )

        for b in candidates:
            if b.id in used_bank_ids:
                continue
            if abs(float(b.amount or 0) - expected_net) > amount_tol:
                continue
            if window_start and b.transaction_date and b.transaction_date < window_start:
                continue
            if window_end and b.transaction_date and b.transaction_date > window_end:
                continue
            bank_hit = b
            break

        if bank_hit:
            used_bank_ids.add(bank_hit.id)
            evidence = {
                "engine": CONTROL_ENGINE,
                "strategy": "reconstruct",
                "match_mode": "contract_expected_vs_bank",
                "decision": "confirmed",
                "confirmed_by": "system_auto_reconstruct",
                "confirmed_at": now.isoformat() + "Z",
                "expected": expected,
                "bank_tx_id": bank_hit.id,
                "bank_amount": bank_hit.amount,
                "provider_tx_id": sale.id,
                "amount_diff": abs(float(bank_hit.amount or 0) - expected_net),
                "rules_fired": [
                    "contract_fee_schedule",
                    "expected_net_vs_bank",
                    "payout_window",
                ],
            }
            ev_json = json.dumps(evidence, ensure_ascii=False)
            bank_hit.matched = 1
            bank_hit.match_status = "confirmed"
            bank_hit.match_evidence = ev_json
            bank_hit.matched_at = now
            sale.matched = 1
            sale.match_status = "confirmed"
            sale.match_evidence = ev_json
            sale.matched_at = now
            sale.matched_bank_tx_id = bank_hit.id
            confirmed += 1
            if len(samples) < 15:
                samples.append(evidence)
            continue

        # Miss → documented discrepancy
        gu = gross_up_from_net(expected_net, terms)  # informational
        evidence = {
            "engine": CONTROL_ENGINE,
            "strategy": "reconstruct",
            "decision": "discrepancy",
            "reject_reason": "expected_net_not_found_in_bank",
            "expected": expected,
            "gross_up_check": gu,
            "provider_tx_id": sale.id,
            "provider_name": sale.provider_name,
            "searched_bank_amount": expected_net,
            "payout_window_end": window_end.isoformat() if window_end else None,
        }
        sale.match_status = "discrepancy"
        sale.match_evidence = json.dumps(evidence, ensure_ascii=False)

        if create_disputes:
            dtype = "late_settlement" if (window_end and window_end < now) else "missing_payout"
            db.add(
                Dispute(
                    tenant_id=tenant_id,
                    provider_name=sale.provider_name or terms.provider_name,
                    dispute_type=dtype,
                    status="open",
                    amount=expected_net,
                    description=(
                        f"[control_reconstruct] provider_tx_id={sale.id} "
                        f"gross={gross} expected_net={expected_net} "
                        f"payout_days={terms.payout_days} "
                        f"evidence={json.dumps(evidence, ensure_ascii=False)[:1500]}"
                    ),
                    expected_resolution=now + timedelta(days=14),
                )
            )
            disputed += 1
        if len(samples) < 15:
            samples.append(evidence)

    await db.commit()

    return {
        "status": "ok",
        "sales_considered": len(sales),
        "confirmed_by_contract": confirmed,
        "discrepancies_opened": disputed,
        "wallet_pending_verification": wallet_pending,
        "unmatched_banks_remaining": sum(
            1 for b in unmatched_banks if b.id not in used_bank_ids and not b.matched
        ),
        "samples": samples,
        "note": (
            "Reconstruct usa contrato (fee% / fijo / IVA / D+X) sobre ventas sin detalle "
            "de liquidadora y las contrasta con abonos banco."
        ),
    }
