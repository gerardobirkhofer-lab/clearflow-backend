from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.database import get_db
from app.core.secrets import decrypt_secret, encrypt_secret, mask_secret
from app.models.bank_account import BankAccount

router = APIRouter()

@router.post("/", status_code=201)
async def create_bank_account(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    plain_iban = (data.get("iban") or "").strip()
    plain_number = (data.get("account_number") or "").strip()
    account = BankAccount(
        tenant_id=current_user.tenant_id,
        name=data.get("name"),
        bank_name=data.get("bank_name"),
        account_number=encrypt_secret(plain_number) if plain_number else None,
        iban=encrypt_secret(plain_iban) if plain_iban else None,
        currency=data.get("currency", "EUR"),
        opening_balance=data.get("opening_balance"),
        is_active=1,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return {
        "id": account.id,
        "name": account.name,
        "bank_name": account.bank_name,
        "account_number": mask_secret(decrypt_secret(account.account_number)),
        "iban": mask_secret(decrypt_secret(account.iban)),
        "currency": account.currency,
        "opening_balance": account.opening_balance,
    }

@router.get("/")
async def list_bank_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(BankAccount)
        .where(BankAccount.is_active == 1, BankAccount.tenant_id == current_user.tenant_id)
        .order_by(desc(BankAccount.created_at))
    )
    accounts = result.scalars().all()
    return {
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "bank_name": a.bank_name,
                "account_number": mask_secret(decrypt_secret(a.account_number)),
                "iban": mask_secret(decrypt_secret(a.iban)),
                "currency": a.currency,
                "opening_balance": a.opening_balance,
            }
            for a in accounts
        ]
    }

@router.delete("/{account_id}")
async def delete_bank_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(BankAccount).where(
            BankAccount.id == account_id,
            BankAccount.tenant_id == current_user.tenant_id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.is_active = 0
    await db.commit()
    return {"message": "Bank account deleted"}
