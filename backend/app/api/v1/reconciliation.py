"""
ClearFlow reconciliation — trust-first auto-confirm (1:1 + 1:N).

Policy:
  - Auto-CONFIRM only with strong evidence (no client click).
  - 1:N when settlement group sum equals bank amount.
  - Near misses (≤ €1) → match_status=suggested, matched=0 (never counted as confirmed).
  - Everything else stays pending.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SharedSessionLocal, get_shared_db
from app.core.tenant import resolve_tenant_id
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction
from app.services.matching_engine import (
    ENGINE,
    evaluate_auto_confirm_1to1,
    evaluate_auto_confirm_1ton,
    index_providers,
    norm_ref,
)
from app.services import recon_jobs

router = APIRouter()

# Back-compat aliases for older imports / tests
evaluate_auto_confirm = evaluate_auto_confirm_1to1


async def execute_reconciliation(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    progress_cb=None,
) -> dict:
    """
    Auto-confirm bank↔provider with 1:1 then 1:N settlement sums.
    Optional date window limits which unmatched bank rows are considered.
    """
    bank_filters = [
        BankTransaction.tenant_id == tenant_id,
        BankTransaction.matched == 0,
    ]
    if date_from is not None:
        bank_filters.append(BankTransaction.transaction_date >= date_from)
    if date_to is not None:
        bank_filters.append(BankTransaction.transaction_date <= date_to)

    prov_result = await db.execute(
        select(
            ProviderTransaction.id,
            ProviderTransaction.amount,
            ProviderTransaction.transaction_date,
            ProviderTransaction.reference,
            ProviderTransaction.concept,
            ProviderTransaction.gross_amount,
            ProviderTransaction.fee_amount,
            ProviderTransaction.net_amount,
            ProviderTransaction.settlement_id,
        ).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_rows = list(prov_result.all())
    prov_by_ref, prov_by_settle = index_providers(prov_rows)

    bank_count = await db.scalar(
        select(func.count()).where(and_(*bank_filters))
    ) or 0

    if progress_cb:
        await progress_cb(5)

    confirmed_pairs = 0  # bank rows confirmed
    confirmed_1n_banks = 0
    confirmed_1n_legs = 0
    suggested_count = 0
    pending_bank = 0
    used_prov_ids: set[int] = set()
    confirmations: list[dict] = []
    suggestions: list[dict] = []
    reject_counts: dict[str, int] = {}
    amount_rule_counts: dict[str, int] = {}

    batch_size = 2000
    offset = 0
    processed = 0
    while offset < bank_count:
        bank_batch = (
            await db.execute(
                select(
                    BankTransaction.id,
                    BankTransaction.amount,
                    BankTransaction.transaction_date,
                    BankTransaction.reference,
                    BankTransaction.concept,
                    BankTransaction.settlement_id,
                )
                .where(and_(*bank_filters))
                .order_by(BankTransaction.id)
                .offset(offset)
                .limit(batch_size)
            )
        ).all()
        if not bank_batch:
            break

        for b_id, b_amount, b_date, b_ref, b_conc, b_settle in bank_batch:
            processed += 1
            nref = norm_ref(b_ref)
            nsettle = norm_ref(b_settle)
            seen_pids: set[int] = set()
            candidates: list = []
            for p in prov_by_ref.get(nref, []):
                if p[0] not in used_prov_ids and p[0] not in seen_pids:
                    candidates.append(p)
                    seen_pids.add(p[0])
            for p in prov_by_settle.get(nsettle, []):
                if p[0] not in used_prov_ids and p[0] not in seen_pids:
                    candidates.append(p)
                    seen_pids.add(p[0])
            # Also: bank reference may be the settlement id of many sale legs
            if nref and nref != nsettle:
                for p in prov_by_settle.get(nref, []):
                    if p[0] not in used_prov_ids and p[0] not in seen_pids:
                        candidates.append(p)
                        seen_pids.add(p[0])

            confirmed = False
            last_reject = "no_provider_candidate" if not candidates else None

            # --- Pass A: 1:1 ---
            for p in candidates:
                (
                    pid, p_amount, p_date, p_ref, p_conc,
                    p_gross, p_fee, p_net, p_settle,
                ) = p
                ok, evidence = evaluate_auto_confirm_1to1(
                    b_amount, b_date, b_ref, b_conc,
                    p_amount, p_date, p_ref, p_conc,
                    p_gross=p_gross, p_fee=p_fee, p_net=p_net,
                    b_settlement_id=b_settle, p_settlement_id=p_settle,
                )
                if not ok:
                    last_reject = evidence.get("reject_reason") or last_reject
                    continue
                evidence["bank_tx_id"] = b_id
                evidence["provider_tx_id"] = pid
                evidence["provider_tx_ids"] = [pid]
                used_prov_ids.add(pid)
                confirmations.append({
                    "bank_id": b_id,
                    "prov_ids": [pid],
                    "evidence": evidence,
                })
                confirmed_pairs += 1
                confirmed = True
                rule = evidence.get("amount_rule") or "exact_amount"
                amount_rule_counts[rule] = amount_rule_counts.get(rule, 0) + 1
                break

            # --- Pass B: 1:N settlement sum ---
            if not confirmed:
                group_key = nsettle or nref
                group: list = []
                if group_key:
                    seen_g: set[int] = set()
                    for p in prov_by_settle.get(group_key, []):
                        if p[0] not in used_prov_ids and p[0] not in seen_g:
                            group.append(p)
                            seen_g.add(p[0])
                    for p in prov_by_ref.get(group_key, []):
                        if p[0] not in used_prov_ids and p[0] not in seen_g:
                            # only if they look like settlement legs (have settle or same ref)
                            if norm_ref(p[8]) == group_key or norm_ref(p[3]) == group_key:
                                group.append(p)
                                seen_g.add(p[0])

                if len(group) >= 2:
                    decision, evidence = evaluate_auto_confirm_1ton(
                        b_amount, b_date, b_ref, b_settle or b_ref, group,
                    )
                    evidence["bank_tx_id"] = b_id
                    if decision == "confirmed":
                        pids = evidence.get("provider_tx_ids") or []
                        for pid in pids:
                            used_prov_ids.add(pid)
                        confirmations.append({
                            "bank_id": b_id,
                            "prov_ids": pids,
                            "evidence": evidence,
                        })
                        confirmed_pairs += 1
                        confirmed_1n_banks += 1
                        confirmed_1n_legs += len(pids)
                        confirmed = True
                        amount_rule_counts["sum_1n"] = amount_rule_counts.get("sum_1n", 0) + 1
                    elif decision == "suggested":
                        pids = evidence.get("provider_tx_ids") or []
                        suggestions.append({
                            "bank_id": b_id,
                            "prov_ids": pids,
                            "evidence": evidence,
                        })
                        suggested_count += 1
                        confirmed = True  # bank handled (as suggested, not pending)
                        last_reject = None
                    else:
                        last_reject = evidence.get("reject_reason") or last_reject

            if not confirmed:
                pending_bank += 1
                if last_reject:
                    reject_counts[last_reject] = reject_counts.get(last_reject, 0) + 1

        offset += batch_size
        if progress_cb and bank_count:
            pct = min(90, 5 + int(85 * processed / max(bank_count, 1)))
            await progress_cb(pct)

    now = datetime.utcnow()
    for c in confirmations:
        ev_json = json.dumps(c["evidence"], ensure_ascii=False)
        await db.execute(
            text(
                """
                UPDATE bank_transactions
                SET matched = 1,
                    match_status = 'confirmed',
                    match_evidence = :ev,
                    matched_at = :ts
                WHERE id = :id
                """
            ),
            {"id": c["bank_id"], "ev": ev_json, "ts": now},
        )
        for pid in c["prov_ids"]:
            await db.execute(
                text(
                    """
                    UPDATE provider_transactions
                    SET matched = 1,
                        match_status = 'confirmed',
                        match_evidence = :ev,
                        matched_at = :ts,
                        matched_bank_tx_id = :bid
                    WHERE id = :pid
                    """
                ),
                {"pid": pid, "bid": c["bank_id"], "ev": ev_json, "ts": now},
            )

    for s in suggestions:
        ev_json = json.dumps(s["evidence"], ensure_ascii=False)
        # Bank stays unmatched=0 but status=suggested for UI honesty
        await db.execute(
            text(
                """
                UPDATE bank_transactions
                SET matched = 0,
                    match_status = 'suggested',
                    match_evidence = :ev,
                    matched_at = :ts
                WHERE id = :id
                """
            ),
            {"id": s["bank_id"], "ev": ev_json, "ts": now},
        )
        for pid in s["prov_ids"]:
            await db.execute(
                text(
                    """
                    UPDATE provider_transactions
                    SET matched = 0,
                        match_status = 'suggested',
                        match_evidence = :ev,
                        matched_at = :ts,
                        matched_bank_tx_id = :bid
                    WHERE id = :pid
                    """
                ),
                {"pid": pid, "bid": s["bank_id"], "ev": ev_json, "ts": now},
            )

    await db.commit()
    if progress_cb:
        await progress_cb(100)

    pending_provider = sum(1 for p in prov_rows if p[0] not in used_prov_ids)

    return {
        "engine": ENGINE,
        "policy": "auto_confirm_1to1_or_1n_settlement_sum",
        "matched_count": confirmed_pairs,
        "confirmed_count": confirmed_pairs,
        "confirmed_1n_bank_count": confirmed_1n_banks,
        "confirmed_1n_leg_count": confirmed_1n_legs,
        "suggested_count": suggested_count,
        "pending_bank_count": pending_bank,
        "pending_provider_count": pending_provider,
        "unmatched_bank_count": pending_bank,
        "unmatched_provider_count": pending_provider,
        "total_bank": bank_count,
        "total_provider": len(prov_rows),
        "reject_reason_counts": reject_counts,
        "amount_rule_counts": amount_rule_counts,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "note": (
            "1:1: ref/settlement + amount/net/gross−fee. "
            "1:N: settlement group sum == bank (±€0.01). "
            "Near sum (≤€1) → suggested (not confirmed). "
            "Rest stays pending."
        ),
    }


def _parse_optional_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value}") from exc


@router.post("/run")
async def run_reconciliation(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_shared_db),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Synchronous trust-first auto-confirm (optional date window)."""
    return await execute_reconciliation(
        db,
        tenant_id,
        date_from=_parse_optional_date(date_from),
        date_to=_parse_optional_date(date_to),
    )


async def _run_job(job_id: str, tenant_id: uuid.UUID, date_from, date_to) -> None:
    await recon_jobs.update_job(job_id, status="running", progress=1)

    async def progress_cb(pct: int):
        await recon_jobs.update_job(job_id, progress=pct)

    try:
        async with SharedSessionLocal() as db:
            result = await execute_reconciliation(
                db,
                tenant_id,
                date_from=date_from,
                date_to=date_to,
                progress_cb=progress_cb,
            )
        await recon_jobs.update_job(
            job_id, status="done", progress=100, result=result
        )
    except Exception as exc:  # noqa: BLE001
        await recon_jobs.update_job(
            job_id, status="error", error=str(exc)[:500], progress=100
        )


@router.post("/run-async")
async def run_reconciliation_async(
    background_tasks: BackgroundTasks,
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Queue reconciliation; poll GET /jobs/{job_id} for progress."""
    job_id = await recon_jobs.create_job(tenant_id)
    background_tasks.add_task(
        _run_job,
        job_id,
        tenant_id,
        _parse_optional_date(date_from),
        _parse_optional_date(date_to),
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "poll": f"/api/v1/reconciliation/jobs/{job_id}",
    }


@router.get("/jobs/{job_id}")
async def get_reconciliation_job(
    job_id: str,
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
):
    job = await recon_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("tenant_id") != str(tenant_id):
        raise HTTPException(status_code=403, detail="Job belongs to another tenant")
    return job


@router.get("/jobs")
async def list_reconciliation_jobs(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Recent persisted recon jobs for this tenant."""
    items = await recon_jobs.list_jobs_for_tenant(tenant_id, limit=limit)
    return {"jobs": items, "count": len(items)}


@router.get("/status")
async def get_reconciliation_status(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_shared_db),
):
    """Counts: confirmed / suggested / pending."""
    bank_transactions = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id
        )
    ) or 0
    provider_transactions = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id
        )
    ) or 0

    confirmed_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 1,
        )
    ) or 0
    confirmed_provider = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 1,
        )
    ) or 0
    suggested_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.match_status == "suggested",
        )
    ) or 0

    return {
        "engine": ENGINE,
        "bank_transactions": bank_transactions,
        "provider_transactions": provider_transactions,
        "matched_bank": confirmed_bank,
        "matched_provider": confirmed_provider,
        "confirmed_bank": confirmed_bank,
        "confirmed_provider": confirmed_provider,
        "suggested_bank": suggested_bank,
        "pending_bank": max(0, bank_transactions - confirmed_bank - suggested_bank),
        "pending_provider": max(0, provider_transactions - confirmed_provider),
        "auto_confirm_only": True,
    }


@router.get("/trial-report")
async def trial_report(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_shared_db),
    sample: int = Query(default=20, ge=1, le=100),
):
    """Pilot report: confirmed vs pending + sample evidence (any tenant)."""
    bank_total = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id
        )
    ) or 0
    prov_total = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id
        )
    ) or 0
    confirmed_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 1,
        )
    ) or 0
    suggested_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.match_status == "suggested",
        )
    ) or 0
    pending_bank = max(0, bank_total - confirmed_bank - suggested_bank)

    confirmed_rows = (
        await db.execute(
            select(
                BankTransaction.id,
                BankTransaction.concept,
                BankTransaction.amount,
                BankTransaction.reference,
                BankTransaction.match_evidence,
                BankTransaction.matched_at,
                BankTransaction.match_status,
            )
            .where(
                BankTransaction.tenant_id == tenant_id,
                BankTransaction.match_status.in_(["confirmed", "suggested"]),
            )
            .order_by(BankTransaction.matched_at.desc().nullslast())
            .limit(sample)
        )
    ).all()

    samples = []
    rule_hist: dict[str, int] = {}
    mode_hist: dict[str, int] = {}
    for row in confirmed_rows:
        ev_raw = row[4]
        ev: dict = {}
        if ev_raw:
            try:
                ev = json.loads(ev_raw) if isinstance(ev_raw, str) else dict(ev_raw)
            except Exception:
                ev = {"raw": str(ev_raw)[:200]}
        for r in ev.get("rules_fired") or []:
            rule_hist[str(r)] = rule_hist.get(str(r), 0) + 1
        mode = str(ev.get("match_mode") or "1:1")
        mode_hist[mode] = mode_hist.get(mode, 0) + 1
        samples.append({
            "bank_tx_id": row[0],
            "concept": (row[1] or "")[:120],
            "amount": row[2],
            "reference": row[3],
            "matched_at": row[5].isoformat() if row[5] else None,
            "match_status": row[6],
            "evidence": {
                "engine": ev.get("engine"),
                "match_mode": ev.get("match_mode"),
                "rules_fired": ev.get("rules_fired"),
                "amount_rule": ev.get("amount_rule"),
                "amount_diff": ev.get("amount_diff"),
                "legs_sum": ev.get("legs_sum"),
                "provider_tx_ids": ev.get("provider_tx_ids"),
                "provider_tx_id": ev.get("provider_tx_id"),
                "day_diff": ev.get("day_diff"),
                "confirmed_by": ev.get("confirmed_by"),
                "decision": ev.get("decision"),
            },
        })

    rate = round((confirmed_bank / bank_total) * 100, 1) if bank_total else 0.0
    return {
        "engine": ENGINE,
        "pilot": True,
        "disclaimer": (
            "Propuesta del motor con evidencia guardada. "
            "No es un cierre de caja firmado ni auditoría externa."
        ),
        "totals": {
            "bank": bank_total,
            "provider": prov_total,
            "confirmed_bank": confirmed_bank,
            "suggested_bank": suggested_bank,
            "pending_bank": pending_bank,
            "confirm_rate_pct": rate,
        },
        "match_mode_histogram": mode_hist,
        "rules_fired_histogram": rule_hist,
        "confirmed_samples": samples,
    }
