"""
ClearFlow reconciliation — trust-first auto-confirm.

Policy:
  - The system auto-CONFIRMS only with strong evidence (no client click).
  - Weak / ambiguous pairs stay PENDING (never shown as reconciled).
  - Every confirmation stores match_evidence JSON for audit.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_shared_db
from app.core.tenant import resolve_tenant_id
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

router = APIRouter()

ENGINE = "clearflow_v2_trust"
# Auto-confirm only when reference is exact AND amount is exact (within 1 cent)
# AND dates are within this many days (if both dates exist).
AUTO_CONFIRM_MAX_DAY_DIFF = 3


def _norm_ref(ref: str | None) -> str:
    if not ref:
        return ""
    return str(ref).strip().upper()


def _day_diff(a, b) -> int | None:
    if not a or not b:
        return None
    try:
        da = a.date() if hasattr(a, "date") else a
        db_ = b.date() if hasattr(b, "date") else b
        return abs((da - db_).days)
    except Exception:
        return None


def evaluate_auto_confirm(
    b_amount, b_date, b_ref, b_conc,
    p_amount, p_date, p_ref, p_conc,
) -> tuple[bool, dict]:
    """
    Returns (should_confirm, evidence).
    Strict: non-empty equal references + amount within €0.01.
    Date alignment strengthens evidence but missing dates do not block if refs+amount hold.
    Fuzzy concept-only matches NEVER auto-confirm.
    """
    evidence: dict = {
        "engine": ENGINE,
        "rules_fired": [],
        "bank_amount": b_amount,
        "provider_amount": p_amount,
        "bank_ref": b_ref,
        "provider_ref": p_ref,
        "bank_date": b_date.isoformat() if b_date else None,
        "provider_date": p_date.isoformat() if p_date else None,
    }

    n_bank = _norm_ref(b_ref)
    n_prov = _norm_ref(p_ref)
    if not n_bank or not n_prov or n_bank != n_prov:
        evidence["reject_reason"] = "missing_or_mismatched_reference"
        return False, evidence
    evidence["rules_fired"].append("exact_reference")

    if b_amount is None or p_amount is None:
        evidence["reject_reason"] = "missing_amount"
        return False, evidence
    amt_diff = abs(float(b_amount) - float(p_amount))
    evidence["amount_diff"] = amt_diff
    if amt_diff >= 0.01:
        evidence["reject_reason"] = "amount_not_exact"
        return False, evidence
    evidence["rules_fired"].append("exact_amount")

    days = _day_diff(b_date, p_date)
    evidence["day_diff"] = days
    if days is not None and days > AUTO_CONFIRM_MAX_DAY_DIFF:
        evidence["reject_reason"] = "dates_too_far"
        return False, evidence
    if days is not None:
        evidence["rules_fired"].append("date_within_window")
    else:
        evidence["rules_fired"].append("date_unavailable_ok_with_ref_amount")

    # Concept similarity is informational only — never required for auto-confirm
    if b_conc and p_conc:
        bc, pc = str(b_conc).lower(), str(p_conc).lower()
        if bc and pc and (bc in pc or pc in bc):
            evidence["rules_fired"].append("concept_overlap_info")

    evidence["decision"] = "confirmed"
    evidence["confirmed_by"] = "system_auto"
    evidence["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
    return True, evidence


async def execute_reconciliation(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """
    Auto-confirm bank↔provider pairs with strong evidence only.
    Used by POST /reconciliation/run and CSV bank upload.
    """
    prov_result = await db.execute(
        select(
            ProviderTransaction.id,
            ProviderTransaction.amount,
            ProviderTransaction.transaction_date,
            ProviderTransaction.reference,
            ProviderTransaction.concept,
        ).where(
            ProviderTransaction.tenant_id == tenant_id,
            ProviderTransaction.matched == 0,
        )
    )
    prov_rows = list(prov_result.all())

    # Index by normalized reference for O(1) strong matches; also amount buckets as fallback scan
    prov_by_ref: dict[str, list] = defaultdict(list)
    for p in prov_rows:
        nref = _norm_ref(p[3])
        if nref:
            prov_by_ref[nref].append(p)

    bank_count = await db.scalar(
        select(func.count()).where(
            BankTransaction.tenant_id == tenant_id,
            BankTransaction.matched == 0,
        )
    ) or 0

    confirmed_count = 0
    pending_bank = 0
    used_prov_ids: set[int] = set()
    confirmations: list[dict] = []  # for bulk update

    batch_size = 2000
    offset = 0
    while offset < bank_count:
        bank_batch = (
            await db.execute(
                select(
                    BankTransaction.id,
                    BankTransaction.amount,
                    BankTransaction.transaction_date,
                    BankTransaction.reference,
                    BankTransaction.concept,
                )
                .where(
                    BankTransaction.tenant_id == tenant_id,
                    BankTransaction.matched == 0,
                )
                .offset(offset)
                .limit(batch_size)
            )
        ).all()
        if not bank_batch:
            break

        for b_id, b_amount, b_date, b_ref, b_conc in bank_batch:
            nref = _norm_ref(b_ref)
            candidates = [p for p in prov_by_ref.get(nref, []) if p[0] not in used_prov_ids]

            confirmed = False
            for p in candidates:
                pid, p_amount, p_date, p_ref, p_conc = p
                ok, evidence = evaluate_auto_confirm(
                    b_amount, b_date, b_ref, b_conc,
                    p_amount, p_date, p_ref, p_conc,
                )
                if not ok:
                    continue
                evidence["bank_tx_id"] = b_id
                evidence["provider_tx_id"] = pid
                used_prov_ids.add(pid)
                confirmations.append({
                    "bank_id": b_id,
                    "prov_id": pid,
                    "evidence": evidence,
                })
                confirmed_count += 1
                confirmed = True
                break

            if not confirmed:
                pending_bank += 1

        offset += batch_size

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
            {"pid": c["prov_id"], "bid": c["bank_id"], "ev": ev_json, "ts": now},
        )

    await db.commit()

    pending_provider = sum(1 for p in prov_rows if p[0] not in used_prov_ids)

    return {
        "engine": ENGINE,
        "policy": "auto_confirm_exact_ref_and_amount_only",
        "matched_count": confirmed_count,  # alias for FE
        "confirmed_count": confirmed_count,
        "pending_bank_count": pending_bank,
        "pending_provider_count": pending_provider,
        "unmatched_bank_count": pending_bank,
        "unmatched_provider_count": pending_provider,
        "total_bank": bank_count,
        "total_provider": len(prov_rows),
        "note": (
            "Solo se auto-confirman pares con referencia exacta e importe exacto. "
            "El resto queda pendiente — sin pedir confirmación al cliente."
        ),
    }


@router.post("/run")
async def run_reconciliation(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_shared_db),
):
    """Run trust-first auto-confirm for the authenticated tenant."""
    return await execute_reconciliation(db, tenant_id)


@router.get("/status")
async def get_reconciliation_status(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_shared_db),
):
    """Counts: confirmed (auto) vs pending — no client confirmation queue."""
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

    # Prefer match_status when column exists; matched=1 remains compatible
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

    return {
        "engine": ENGINE,
        "bank_transactions": bank_transactions,
        "provider_transactions": provider_transactions,
        "matched_bank": confirmed_bank,
        "matched_provider": confirmed_provider,
        "confirmed_bank": confirmed_bank,
        "confirmed_provider": confirmed_provider,
        "pending_bank": max(0, bank_transactions - confirmed_bank),
        "pending_provider": max(0, provider_transactions - confirmed_provider),
        "auto_confirm_only": True,
    }
