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
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db, tenant_db_manager
from app.core.tenant import resolve_tenant_id
from app.models.bank_transaction import BankTransaction
from app.models.cash_close import CashClose
from app.models.dispute import Dispute
from app.models.provider import Provider
from app.models.provider_transaction import ProviderTransaction
from app.services.contract_math import ContractTerms
from app.services.contractual_audit import apply_contract_recalc
from app.services.matching_engine import (
    ENGINE,
    evaluate_auto_confirm_1to1,
    evaluate_auto_confirm_1ton,
    index_providers,
    norm_ref,
)
from app.services import recon_jobs

router = APIRouter()

# Back-compat: older code expected (bool, evidence)
def evaluate_auto_confirm(*args, **kwargs):
    decision, evidence = evaluate_auto_confirm_1to1(*args, **kwargs)
    return decision == "confirmed", evidence


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

    # Ensure contract expected nets are stamped before matching
    await _ensure_contract_audit(db, tenant_id)

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
            ProviderTransaction.contract_expected_net,
            ProviderTransaction.gateway_reported_net,
            ProviderTransaction.contract_audit_status,
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
    contractual_blocks: list[dict] = []
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
                    c_net, g_net, c_status,
                ) = p
                decision_1, evidence = evaluate_auto_confirm_1to1(
                    b_amount, b_date, b_ref, b_conc,
                    p_amount, p_date, p_ref, p_conc,
                    p_gross=p_gross, p_fee=p_fee, p_net=p_net,
                    b_settlement_id=b_settle, p_settlement_id=p_settle,
                    contract_expected_net=c_net,
                    gateway_reported_net=g_net,
                    contract_audit_status=c_status,
                )
                if decision_1 == "reject":
                    last_reject = evidence.get("reject_reason") or last_reject
                    if evidence.get("decision") == "blocked_contractual_discrepancy" or (
                        last_reject
                        and "contract" in str(last_reject)
                    ):
                        contractual_blocks.append({
                            "bank_id": b_id,
                            "prov_id": pid,
                            "evidence": evidence,
                        })
                    continue
                evidence["bank_tx_id"] = b_id
                evidence["provider_tx_id"] = pid
                evidence["provider_tx_ids"] = [pid]
                if decision_1 == "suggested":
                    suggestions.append({
                        "bank_id": b_id,
                        "prov_ids": [pid],
                        "evidence": evidence,
                    })
                    suggested_count += 1
                    confirmed = True
                    last_reject = None
                    break
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
                        if evidence.get("decision") == "blocked_contractual_discrepancy" or (
                            last_reject and "contract" in str(last_reject)
                        ):
                            contractual_blocks.append({
                                "bank_id": b_id,
                                "prov_ids": evidence.get("provider_tx_ids") or [],
                                "evidence": evidence,
                            })

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

    # Persist contractual discrepancies (blocked auto-confirm)
    contractual_opened = await _open_contractual_disputes(db, tenant_id, contractual_blocks)

    await db.commit()
    if progress_cb:
        await progress_cb(100)

    pending_provider = sum(1 for p in prov_rows if p[0] not in used_prov_ids)

    return {
        "engine": ENGINE,
        "policy": "auto_confirm_only_if_bank_matches_contract_expected_net",
        "matched_count": confirmed_pairs,
        "confirmed_count": confirmed_pairs,
        "confirmed_1n_bank_count": confirmed_1n_banks,
        "confirmed_1n_leg_count": confirmed_1n_legs,
        "suggested_count": suggested_count,
        "contractual_blocks": len(contractual_blocks),
        "contractual_discrepancies_opened": contractual_opened,
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
            "Auditoría contractual: el banco debe cuadrar con el neto recalculado "
            "desde el contrato (no con el neto declarado por la pasarela). "
            "Desviaciones → bloqueo de auto-confirm + Discrepancia Contractual."
        ),
    }


async def _load_terms_map(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, ContractTerms]:
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
        out[t.provider_name.lower()] = t
        if p.provider_type:
            out[p.provider_type.lower()] = t
    return out


async def _ensure_contract_audit(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Stamp contract_expected_* on provider txs missing audit."""
    terms_map = await _load_terms_map(db, tenant_id)
    txs = (
        await db.execute(
            select(ProviderTransaction).where(
                ProviderTransaction.tenant_id == tenant_id,
                ProviderTransaction.matched == 0,
                or_(
                    ProviderTransaction.contract_expected_net.is_(None),
                    ProviderTransaction.contract_audit_status == "pending_audit",
                ),
            ).limit(5000)
        )
    ).scalars().all()
    n = 0
    for tx in txs:
        pname = (tx.provider_name or "").lower()
        terms = terms_map.get(pname)
        if not terms:
            for k, t in terms_map.items():
                if k and (k in pname or pname in k):
                    terms = t
                    break
        fields = apply_contract_recalc(
            amount=tx.amount,
            gross_amount=tx.gross_amount,
            fee_amount=tx.fee_amount,
            net_amount=tx.net_amount,
            terms=terms,
            sale_date=tx.transaction_date,
        )
        tx.gateway_reported_net = fields["gateway_reported_net"]
        tx.contract_gross_basis = fields["contract_gross_basis"]
        tx.contract_expected_fee = fields["contract_expected_fee"]
        tx.contract_expected_net = fields["contract_expected_net"]
        tx.contract_audit_status = fields["contract_audit_status"]
        tx.contract_audit_json = fields["contract_audit_json"]
        n += 1
    if n:
        await db.commit()
    return n


async def _open_contractual_disputes(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    blocks: list[dict],
) -> int:
    opened = 0
    now = datetime.utcnow()
    seen: set[str] = set()
    for b in blocks:
        ev = b.get("evidence") or {}
        key = f"{b.get('bank_id')}:{b.get('prov_id') or tuple(b.get('prov_ids') or [])}"
        if key in seen:
            continue
        seen.add(key)
        diff = ev.get("amount_diff")
        expected = ev.get("contract_expected_net") or ev.get("contract_sum")
        bank_amt = ev.get("bank_amount")
        reason = ev.get("reject_reason") or "contractual_mismatch"
        amount_val = (
            float(expected)
            if expected is not None
            else (float(bank_amt) if bank_amt is not None else 0)
        )
        desc = (
            f"[Discrepancia Contractual] {reason} "
            f"bank_id={b.get('bank_id')} bank_amount={bank_amt} "
            f"contract_expected={expected} amount_diff={diff} "
            f"gateway_net={ev.get('gateway_reported_net')} "
            f"evidence={json.dumps(ev, ensure_ascii=False)[:1200]}"
        )
        dispute = Dispute(
            tenant_id=tenant_id,
            transaction_id=b.get("bank_id"),
            provider_name=str(ev.get("provider_ref") or "provider")[:50],
            dispute_type="contractual_discrepancy",
            status="open",
            amount=amount_val,
            description=desc,
            expected_resolution=now + timedelta(days=14),
        )
        db.add(dispute)
        await db.flush()
        # Draft email for client approval — never auto-send
        from app.services.dispute_email_draft import enqueue_dispute_email_draft

        await enqueue_dispute_email_draft(
            db,
            tenant_id=tenant_id,
            dispute_id=dispute.id,
            provider_name=dispute.provider_name or "provider",
            dispute_type="contractual_discrepancy",
            amount=amount_val,
            description=desc,
            meta={"bank_id": b.get("bank_id"), "reject_reason": reason},
        )
        # Mark provider legs if single
        pid = b.get("prov_id")
        if pid:
            await db.execute(
                text(
                    """
                    UPDATE provider_transactions
                    SET match_status = 'contractual_discrepancy',
                        match_evidence = :ev
                    WHERE id = :pid AND matched = 0
                    """
                ),
                {"pid": pid, "ev": json.dumps(ev, ensure_ascii=False)},
            )
        for pid2 in b.get("prov_ids") or []:
            await db.execute(
                text(
                    """
                    UPDATE provider_transactions
                    SET match_status = 'contractual_discrepancy',
                        match_evidence = :ev
                    WHERE id = :pid AND matched = 0
                    """
                ),
                {"pid": pid2, "ev": json.dumps(ev, ensure_ascii=False)},
            )
        # Mark bank
        if b.get("bank_id"):
            await db.execute(
                text(
                    """
                    UPDATE bank_transactions
                    SET match_status = 'contractual_discrepancy',
                        match_evidence = :ev
                    WHERE id = :id AND matched = 0
                    """
                ),
                {"id": b["bank_id"], "ev": json.dumps(ev, ensure_ascii=False)},
            )
        opened += 1
    return opened


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
    db: AsyncSession = Depends(get_db),
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
        maker = await tenant_db_manager.get_session_maker(tenant_id)
        async with maker() as db:
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
    df = _parse_optional_date(date_from)
    dt = _parse_optional_date(date_to)
    job_id = await recon_jobs.create_job(
        tenant_id,
        params={
            "date_from": date_from,
            "date_to": date_to,
            "engine": ENGINE,
        },
    )
    background_tasks.add_task(_run_job, job_id, tenant_id, df, dt)
    return {
        "job_id": job_id,
        "status": "queued",
        "poll": f"/api/v1/reconciliation/jobs/{job_id}",
        "date_from": date_from,
        "date_to": date_to,
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


async def _window_filters(date_from: datetime | None, date_to: datetime | None):
    filters = []
    if date_from is not None:
        filters.append(BankTransaction.transaction_date >= date_from)
    if date_to is not None:
        filters.append(BankTransaction.transaction_date <= date_to)
    return filters


@router.get("/status")
async def get_reconciliation_status(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Counts: motor-confirmed / user-confirmed / suggested / pending."""
    df = _parse_optional_date(date_from)
    dt = _parse_optional_date(date_to)
    base = [BankTransaction.tenant_id == tenant_id, *(await _window_filters(df, dt))]

    bank_transactions = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(*base)
    ) or 0
    provider_transactions = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id
        )
    ) or 0

    motor_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base,
            BankTransaction.match_status == "confirmed",
            BankTransaction.matched == 1,
        )
    ) or 0
    user_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base,
            BankTransaction.match_status == "user_confirmed",
            BankTransaction.matched == 1,
        )
    ) or 0
    confirmed_bank = motor_confirmed + user_confirmed
    confirmed_provider = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 1,
        )
    ) or 0
    suggested_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base,
            BankTransaction.match_status == "suggested",
        )
    ) or 0
    rejected_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base,
            BankTransaction.match_status == "rejected",
        )
    ) or 0

    return {
        "engine": ENGINE,
        "bank_transactions": bank_transactions,
        "provider_transactions": provider_transactions,
        "matched_bank": confirmed_bank,
        "matched_provider": confirmed_provider,
        "confirmed_bank": confirmed_bank,
        "motor_confirmed_bank": motor_confirmed,
        "user_confirmed_bank": user_confirmed,
        "confirmed_provider": confirmed_provider,
        "suggested_bank": suggested_bank,
        "rejected_bank": rejected_bank,
        "pending_bank": max(
            0, bank_transactions - confirmed_bank - suggested_bank - rejected_bank
        ),
        "pending_provider": max(0, provider_transactions - confirmed_provider),
        "auto_confirm_only": False,
        "policy": "motor_proposes; client confirms suggestions; signed-close seals a period",
    }


@router.post("/suggestions/{bank_tx_id}/confirm")
async def confirm_suggestion(
    bank_tx_id: int,
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Client validates a motor suggestion → user_confirmed (matched=1)."""
    row = (
        await db.execute(
            select(BankTransaction).where(
                BankTransaction.id == bank_tx_id,
                BankTransaction.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Bank transaction not found")
    if row.match_status not in ("suggested", "pending"):
        raise HTTPException(
            400, f"Cannot confirm status={row.match_status}; expected suggested/pending"
        )

    ev: dict = {}
    if row.match_evidence:
        try:
            ev = json.loads(row.match_evidence)
        except Exception:
            ev = {"prior": str(row.match_evidence)[:300]}
    now = datetime.utcnow()
    ev["decision"] = "user_confirmed"
    ev["confirmed_by"] = "user"
    ev["validated_by_user_id"] = str(getattr(current_user, "id", "") or "")
    ev["validated_by_email"] = getattr(current_user, "email", None)
    ev["validated_at"] = now.isoformat() + "Z"
    ev["rules_fired"] = list(ev.get("rules_fired") or []) + ["user_confirmed_suggestion"]

    row.matched = 1
    row.match_status = "user_confirmed"
    row.match_evidence = json.dumps(ev, ensure_ascii=False)
    row.matched_at = now

    # Promote linked provider legs if present
    prov_ids = ev.get("provider_tx_ids") or (
        [ev["provider_tx_id"]] if ev.get("provider_tx_id") is not None else []
    )
    for pid in prov_ids:
        p = await db.get(ProviderTransaction, int(pid))
        if p and p.tenant_id == tenant_id:
            p.matched = 1
            p.match_status = "user_confirmed"
            p.match_evidence = row.match_evidence
            p.matched_at = now
            p.matched_bank_tx_id = bank_tx_id

    await db.commit()
    return {
        "bank_tx_id": bank_tx_id,
        "match_status": "user_confirmed",
        "message": "Sugerencia confirmada por el cliente. Ya cuenta como validada.",
        "evidence": ev,
    }


@router.post("/suggestions/{bank_tx_id}/reject")
async def reject_suggestion(
    bank_tx_id: int,
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Client rejects a motor suggestion — stays unmatched, status=rejected."""
    row = (
        await db.execute(
            select(BankTransaction).where(
                BankTransaction.id == bank_tx_id,
                BankTransaction.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Bank transaction not found")
    if row.match_status not in ("suggested", "pending"):
        raise HTTPException(
            400, f"Cannot reject status={row.match_status}; expected suggested/pending"
        )

    ev: dict = {}
    if row.match_evidence:
        try:
            ev = json.loads(row.match_evidence)
        except Exception:
            ev = {"prior": str(row.match_evidence)[:300]}
    now = datetime.utcnow()
    ev["decision"] = "rejected"
    ev["rejected_by"] = "user"
    ev["validated_by_user_id"] = str(getattr(current_user, "id", "") or "")
    ev["validated_by_email"] = getattr(current_user, "email", None)
    ev["rejected_at"] = now.isoformat() + "Z"
    ev["rules_fired"] = list(ev.get("rules_fired") or []) + ["user_rejected_suggestion"]

    row.matched = 0
    row.match_status = "rejected"
    row.match_evidence = json.dumps(ev, ensure_ascii=False)
    row.matched_at = now

    prov_ids = ev.get("provider_tx_ids") or (
        [ev["provider_tx_id"]] if ev.get("provider_tx_id") is not None else []
    )
    for pid in prov_ids:
        p = await db.get(ProviderTransaction, int(pid))
        if p and p.tenant_id == tenant_id:
            p.matched = 0
            p.match_status = "rejected"
            p.match_evidence = row.match_evidence
            p.matched_at = now
            p.matched_bank_tx_id = None

    await db.commit()
    return {
        "bank_tx_id": bank_tx_id,
        "match_status": "rejected",
        "message": "Sugerencia rechazada. Queda documentada; no cuenta como confirmada.",
        "evidence": ev,
    }


@router.post("/signed-close")
async def signed_close(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    allow_open_items: bool = Query(
        False,
        description="If true, allow close with suggested/pending remaining",
    ),
    note: str | None = Query(None),
):
    """
    Seal a period: snapshot motor vs user-validated counts.
    Default: refuse if suggested/pending remain in the window.
    """
    import hashlib

    df = _parse_optional_date(date_from)
    dt = _parse_optional_date(date_to)
    if not df or not dt:
        raise HTTPException(422, "date_from and date_to required (YYYY-MM-DD)")
    if df.date() > dt.date():
        raise HTTPException(400, "date_from must be <= date_to")

    base = [
        BankTransaction.tenant_id == tenant_id,
        BankTransaction.transaction_date >= df,
        BankTransaction.transaction_date <= dt + timedelta(days=1) - timedelta(seconds=1),
    ]

    bank_total = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(*base)
    ) or 0
    motor_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "confirmed", BankTransaction.matched == 1
        )
    ) or 0
    user_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "user_confirmed", BankTransaction.matched == 1
        )
    ) or 0
    suggested = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "suggested"
        )
    ) or 0
    pending = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base,
            or_(
                BankTransaction.match_status == "pending",
                BankTransaction.match_status.is_(None),
            ),
            BankTransaction.matched == 0,
        )
    ) or 0

    if not allow_open_items and (suggested > 0 or pending > 0):
        raise HTTPException(
            409,
            detail={
                "message": (
                    "Hay sugeridos o pendientes en el periodo. "
                    "Confirmá/rechazá sugerencias o pass allow_open_items=true."
                ),
                "suggested": suggested,
                "pending": pending,
            },
        )

    snapshot = {
        "engine": ENGINE,
        "date_from": date_from,
        "date_to": date_to,
        "bank_total": bank_total,
        "motor_confirmed": motor_confirmed,
        "user_confirmed": user_confirmed,
        "suggested_remaining": suggested,
        "pending_remaining": pending,
        "signed_by_email": getattr(current_user, "email", None),
        "note": note,
    }
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    evidence_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    close = CashClose(
        tenant_id=tenant_id,
        date_from=df.date(),
        date_to=dt.date(),
        status="signed",
        signed_by_user_id=str(getattr(current_user, "id", "") or ""),
        signed_by_email=getattr(current_user, "email", None),
        signed_at=datetime.utcnow(),
        motor_confirmed=motor_confirmed,
        user_confirmed=user_confirmed,
        suggested_remaining=suggested,
        pending_remaining=pending,
        bank_total=bank_total,
        allow_open_items=1 if allow_open_items else 0,
        snapshot_json=blob,
        evidence_hash=evidence_hash,
        note=note,
    )
    db.add(close)
    await db.commit()
    await db.refresh(close)

    return {
        "id": close.id,
        "status": "signed",
        "evidence_hash": evidence_hash,
        "snapshot": snapshot,
        "disclaimer": (
            "Cierre firmado por el usuario del tenant. "
            "No sustituye auditoría externa ni certificación contable."
        ),
    }


@router.get("/signed-closes")
async def list_signed_closes(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    rows = (
        await db.execute(
            select(CashClose)
            .where(CashClose.tenant_id == tenant_id)
            .order_by(CashClose.signed_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "closes": [
            {
                "id": r.id,
                "date_from": r.date_from.isoformat() if r.date_from else None,
                "date_to": r.date_to.isoformat() if r.date_to else None,
                "status": r.status,
                "signed_by_email": r.signed_by_email,
                "signed_at": r.signed_at.isoformat() if r.signed_at else None,
                "motor_confirmed": r.motor_confirmed,
                "user_confirmed": r.user_confirmed,
                "suggested_remaining": r.suggested_remaining,
                "pending_remaining": r.pending_remaining,
                "bank_total": r.bank_total,
                "evidence_hash": r.evidence_hash,
                "note": r.note,
            }
            for r in rows
        ]
    }


@router.get("/trial-report")
async def trial_report(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    sample: int = Query(default=20, ge=1, le=100),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Pilot report: motor vs user-validated + sample evidence."""
    df = _parse_optional_date(date_from)
    dt = _parse_optional_date(date_to)
    base = [BankTransaction.tenant_id == tenant_id, *(await _window_filters(df, dt))]

    bank_total = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(*base)
    ) or 0
    prov_total = await db.scalar(
        select(func.count()).select_from(ProviderTransaction).where(
            ProviderTransaction.tenant_id == tenant_id
        )
    ) or 0
    motor_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "confirmed", BankTransaction.matched == 1
        )
    ) or 0
    user_confirmed = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "user_confirmed", BankTransaction.matched == 1
        )
    ) or 0
    confirmed_bank = motor_confirmed + user_confirmed
    suggested_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "suggested"
        )
    ) or 0
    rejected_bank = await db.scalar(
        select(func.count()).select_from(BankTransaction).where(
            *base, BankTransaction.match_status == "rejected"
        )
    ) or 0
    pending_bank = max(0, bank_total - confirmed_bank - suggested_bank - rejected_bank)

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
                *base,
                BankTransaction.match_status.in_(
                    ["confirmed", "user_confirmed", "suggested", "rejected"]
                ),
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
                "confirmed_by": ev.get("confirmed_by") or ev.get("validated_by_email"),
                "score": ev.get("score"),
                "decision": ev.get("decision"),
            },
        })

    rate = round((confirmed_bank / bank_total) * 100, 1) if bank_total else 0.0
    return {
        "engine": ENGINE,
        "pilot": True,
        "date_from": date_from,
        "date_to": date_to,
        "disclaimer": (
            "Propuesta del motor vs validación del cliente. "
            "Usá signed-close para sellar un periodo. "
            "No es auditoría externa."
        ),
        "totals": {
            "bank": bank_total,
            "provider": prov_total,
            "confirmed_bank": confirmed_bank,
            "motor_confirmed_bank": motor_confirmed,
            "user_confirmed_bank": user_confirmed,
            "suggested_bank": suggested_bank,
            "rejected_bank": rejected_bank,
            "pending_bank": pending_bank,
            "confirm_rate_pct": rate,
        },
        "match_mode_histogram": mode_hist,
        "rules_fired_histogram": rule_hist,
        "confirmed_samples": samples,
    }
