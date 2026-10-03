"""Bank connections are not live yet. This list stays empty until a real provider is approved."""
from fastapi import APIRouter, Depends

from app.core.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/institutions")


@router.get("")
async def list_institutions(current_user: CurrentUser = Depends(get_current_user)):
    return {"items": [], "bank_connection": "unavailable"}
