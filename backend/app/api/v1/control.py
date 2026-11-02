"""
Control de caja — single entrypoint for Detail + Reconstruct strategies.
"""
from __future__ import annotations

import uuid

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import resolve_tenant_id
from app.services.control_orchestrator import CONTROL_ENGINE, run_control
from app.services.contract_math import ContractTerms, expected_from_gross, gross_up_from_net
from app.models.provider import Provider
from sqlalchemy import select

router = APIRouter()


def _parse_optional_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value}") from exc


@router.post("/run")
async def control_run(
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
    detail: bool = Query(default=True),
    reconstruct: bool = Query(default=True),
    create_disputes: bool = Query(default=True),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """
    One control run:
      1) Strategy Detail (line matching) if detail=true
      2) Strategy Reconstruct (contract math vs bank) on remaining gaps
    Optional date window applies to both strategies.
    """
    result = await run_control(
        db,
        tenant_id,
        run_detail=detail,
        run_reconstruct=reconstruct,
        create_disputes=create_disputes,
        date_from=_parse_optional_date(date_from),
        date_to=_parse_optional_date(date_to),
    )
    return result


@router.get("/doctrine")
async def control_doctrine():
    return {
        "engine": CONTROL_ENGINE,
        "keys": ["banco", "wallet", "facturador", "contrato"],
        "strategies": {
            "detail": "Conciliación con detalle de liquidadora (1:1 / 1:N).",
            "reconstruct": (
                "Sin detalle: bruto (facturación/cobros) + contrato → neto/fecha "
                "esperados → contraste con banco/wallet. Gross-up si solo hay neto."
            ),
        },
        "rule": (
            "Hay detalle usable → Detail. No hay → Reconstruct. "
            "Siempre documentado. La opacidad de la liquidadora no impide el control."
        ),
    }


@router.post("/preview-math")
async def preview_contract_math(
    gross: float | None = None,
    net: float | None = None,
    provider_name: str = "stripe",
    tenant_id: uuid.UUID = Depends(resolve_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Quick calculator from persisted contract terms (or defaults)."""
    result = await db.execute(
        select(Provider).where(
            Provider.tenant_id == tenant_id,
            Provider.name.ilike(provider_name),
        )
    )
    p = result.scalar_one_or_none()
    terms = ContractTerms.from_provider(p) if p else ContractTerms(provider_name=provider_name)
    out: dict = {"terms": terms.__dict__}
    if gross is not None:
        out["direct"] = expected_from_gross(gross, None, terms)
    if net is not None:
        out["gross_up"] = gross_up_from_net(net, terms)
    return out
