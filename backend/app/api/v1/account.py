"""Persisted company setup for the signed-in tenant."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.auth import CurrentUser, get_current_user
from app.core.database import SharedSessionLocal
from app.models.account_profile import AccountProfile

router = APIRouter()


def _empty() -> dict:
    return {"payload": {}, "onboarding_complete": False}


@router.get("/profile")
async def get_profile(current_user: CurrentUser = Depends(get_current_user)):
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(AccountProfile).where(AccountProfile.tenant_id == current_user.tenant_id)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return _empty()
    try:
        payload = json.loads(row.payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {"payload": payload, "onboarding_complete": bool(row.onboarding_complete)}


@router.put("/profile")
async def save_profile(data: dict, current_user: CurrentUser = Depends(get_current_user)):
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    complete = bool(data.get("onboarding_complete"))
    encoded = json.dumps(payload, default=str)
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(AccountProfile).where(AccountProfile.tenant_id == current_user.tenant_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = AccountProfile(
                tenant_id=current_user.tenant_id,
                payload=encoded,
                onboarding_complete=1 if complete else 0,
            )
            session.add(row)
        else:
            row.payload = encoded
            row.onboarding_complete = 1 if complete else 0
        await session.commit()
    return {"payload": payload, "onboarding_complete": complete}
