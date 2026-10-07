"""Morning panel and cash health for every place the owner can open."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.services.panel_view import build_caja, build_panel

router = APIRouter()


@router.get("/panel")
async def morning_panel(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await build_panel(db, current_user)


@router.get("/caja")
async def cash_health(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await build_caja(db, current_user)
