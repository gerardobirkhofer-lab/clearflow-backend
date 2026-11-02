"""
Cash flow proyectado + P&L proyectado vs real (datos reales, sin demo).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.tenant import assert_tenant_access
from app.services.projection_service import pnl_projected_vs_actual, projected_cash_flow

router = APIRouter()


@router.get("/cash-flow/projected")
async def get_projected_cash_flow(
    tenant_id: uuid.UUID = Query(...),
    horizon_days: int = Query(30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    # Always compute 7/14/30 for UI toggles; also include requested if custom
    base = (7, 14, 30)
    horizons = tuple(sorted(set(base + (horizon_days,))))
    data = await projected_cash_flow(db, tenant_id, horizons=horizons)
    data["requested_horizon_days"] = horizon_days
    return data


@router.get("/cash-flow/pnl")
async def get_pnl_projected_vs_actual(
    tenant_id: uuid.UUID = Query(...),
    start_date: str | None = Query(None, description="YYYY-MM-DD"),
    end_date: str | None = Query(None, description="YYYY-MM-DD"),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tenant_id = assert_tenant_access(tenant_id, current_user)
    try:
        if end_date:
            end = date.fromisoformat(end_date)
        else:
            end = date.today()
        if start_date:
            start = date.fromisoformat(start_date)
        else:
            start = end - timedelta(days=days - 1)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date: {e}") from e
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    return await pnl_projected_vs_actual(db, tenant_id, start=start, end=end)


@router.get("/cash-flow/health")
async def cash_flow_module_health():
    return {
        "module": "cash_flow",
        "version": "projection_v1",
        "endpoints": ["/cash-flow/projected", "/cash-flow/pnl"],
        "as_of": datetime.utcnow().isoformat() + "Z",
    }
