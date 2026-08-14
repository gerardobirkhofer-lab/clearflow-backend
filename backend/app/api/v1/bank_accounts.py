from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.bank_account import BankAccount

router = APIRouter()

@router.post("/", status_code=201)
async def create_bank_account(data: dict, db: AsyncSession = Depends(get_db)):
    account = BankAccount(
        name=data.get("name"),
        bank_name=data.get("bank_name"),
        account_number=data.get("account_number"),
        iban=data.get("iban"),
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
        "account_number": account.account_number,
        "iban": account.iban,
        "currency": account.currency,
        "opening_balance": account.opening_balance,
    }

@router.get("/")
async def list_bank_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BankAccount).where(BankAccount.is_active == 1).order_by(desc(BankAccount.created_at)))
    accounts = result.scalars().all()
    return {
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "bank_name": a.bank_name,
                "account_number": a.account_number,
                "iban": a.iban,
                "currency": a.currency,
                "opening_balance": a.opening_balance,
            }
            for a in accounts
        ]
    }

@router.delete("/{account_id}")
async def delete_bank_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BankAccount).where(BankAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.is_active = 0
    await db.commit()
    return {"message": "Bank account deleted"}
