"""
Auditoría Contractual y Recálculo Independiente.

Never trust gateway-reported net blindly. Recalculate expected settlement
from tenant contract fee rules (percent + fixed + optional IVA).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.services.contract_math import ContractTerms, fee_on_gross, expected_from_gross

CONTRACT_AUDIT_TOLERANCE = 0.02  # € — gateway vs contract
ENGINE_TAG = "contractual_audit_v1"


def resolve_gross(amount: float | None, gross: float | None, net: float | None) -> float:
    """Prefer explicit gross; else amount if it looks like gross; else net as last resort."""
    if gross is not None and abs(float(gross)) > 0:
        return float(gross)
    if amount is not None and abs(float(amount)) > 0:
        return float(amount)
    if net is not None:
        return float(net)
    return 0.0


async def load_provider_terms(db: Any, tenant_id: Any, provider_name: str) -> ContractTerms | None:
    """Best-effort load of ContractTerms for a provider name within a tenant."""
    from sqlalchemy import or_, select

    from app.models.provider import Provider

    name = (provider_name or "").strip()
    if not name:
        return None
    row = await db.execute(
        select(Provider).where(
            Provider.tenant_id == tenant_id,
            Provider.is_active == 1,
            or_(
                Provider.name.ilike(name),
                Provider.provider_type.ilike(name),
            ),
        ).limit(1)
    )
    p = row.scalar_one_or_none()
    if p is None:
        # Fuzzy: any provider whose name appears in the string
        all_p = (
            await db.execute(
                select(Provider).where(
                    Provider.tenant_id == tenant_id,
                    Provider.is_active == 1,
                )
            )
        ).scalars().all()
        lower = name.lower()
        for cand in all_p:
            cn = (cand.name or "").lower()
            ct = (cand.provider_type or "").lower()
            if cn and (cn in lower or lower in cn):
                p = cand
                break
            if ct and (ct in lower or lower in ct):
                p = cand
                break
    return ContractTerms.from_provider(p) if p else None


def apply_contract_recalc(
    *,
    amount: float | None,
    gross_amount: float | None,
    fee_amount: float | None,
    net_amount: float | None,
    terms: ContractTerms | None,
    sale_date: datetime | None = None,
) -> dict[str, Any]:
    """
    Returns fields to stamp on ProviderTransaction + audit verdict.
    """
    gateway_net = float(net_amount) if net_amount is not None else None
    if gateway_net is None and fee_amount is not None and gross_amount is not None:
        gateway_net = float(gross_amount) - float(fee_amount)

    gross = resolve_gross(amount, gross_amount, net_amount)

    if terms is None:
        return {
            "gateway_reported_net": gateway_net,
            "contract_gross_basis": gross if gross else None,
            "contract_expected_fee": None,
            "contract_expected_net": None,
            "contract_audit_status": "no_contract",
            "contract_audit_json": json.dumps(
                {
                    "engine": ENGINE_TAG,
                    "status": "no_contract",
                    "note": "Sin reglas contractuales para este proveedor/tenant.",
                },
                ensure_ascii=False,
            ),
        }

    calc = expected_from_gross(gross, sale_date, terms)
    expected_net = float(calc["net"])
    expected_fee = float(calc["fee_total"])

    status = "ok"
    deviation = None
    if gateway_net is not None:
        deviation = abs(gateway_net - expected_net)
        if deviation > CONTRACT_AUDIT_TOLERANCE:
            status = "gateway_deviation"

    audit = {
        "engine": ENGINE_TAG,
        "status": status,
        "contract": {
            "provider": terms.provider_name,
            "fee_percent": terms.fee_percent,
            "fee_fixed": terms.fee_fixed,
            "has_iva": terms.has_iva,
            "iva_percent": terms.iva_percent,
            "payout_days": terms.payout_days,
        },
        "gross_basis": gross,
        "contract_expected_fee": expected_fee,
        "contract_expected_net": expected_net,
        "gateway_reported_net": gateway_net,
        "gateway_vs_contract_diff": deviation,
        "tolerance": CONTRACT_AUDIT_TOLERANCE,
        "calculated_at": datetime.utcnow().isoformat() + "Z",
        "note": (
            "Neto recalculado de forma independiente desde el contrato. "
            "No se confía ciegamente en la pasarela."
        ),
    }

    return {
        "gateway_reported_net": gateway_net,
        "contract_gross_basis": gross,
        "contract_expected_fee": expected_fee,
        "contract_expected_net": expected_net,
        "contract_audit_status": status,
        "contract_audit_json": json.dumps(audit, ensure_ascii=False),
    }


def bank_matches_contract_net(
    bank_amount: float | None,
    contract_expected_net: float | None,
    *,
    tol: float = 0.01,
) -> bool:
    if bank_amount is None or contract_expected_net is None:
        return False
    return abs(float(bank_amount) - float(contract_expected_net)) < tol


def contractual_block_reason(
    bank_amount: float | None,
    contract_expected_net: float | None,
    gateway_net: float | None,
    audit_status: str | None,
) -> str | None:
    """
    If auto-confirm must be blocked for contractual reasons, return reason code.
    """
    if audit_status == "gateway_deviation":
        # Still allow confirm IF bank equals contract expected (gateway lied, bank OK)
        if bank_matches_contract_net(bank_amount, contract_expected_net):
            return None
        if gateway_net is not None and bank_amount is not None:
            if abs(float(bank_amount) - float(gateway_net)) < 0.01:
                return "bank_matches_gateway_but_not_contract"
        return "gateway_deviates_from_contract_and_bank_unverified"

    if contract_expected_net is not None and bank_amount is not None:
        if not bank_matches_contract_net(bank_amount, contract_expected_net):
            # Bank may still match gateway — that is also a block if we have contract
            if gateway_net is not None and abs(float(bank_amount) - float(gateway_net)) < 0.01:
                return "bank_matches_gateway_but_not_contract"
            return "bank_differs_from_contract_expected_net"

    return None
