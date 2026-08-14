from fastapi import APIRouter

router = APIRouter(prefix="/institutions")

@router.get("")
async def list_institutions():
    return [
        {"id": "inst-bbva", "name": "BBVA", "country": "ES", "status": "ACTIVE", "last_sync": "2026-08-04T14:30:00Z", "account_count": 3},
        {"id": "inst-sant", "name": "Santander", "country": "ES", "status": "ACTIVE", "last_sync": "2026-08-04T13:45:00Z", "account_count": 2},
        {"id": "inst-db", "name": "Deutsche Bank", "country": "DE", "status": "SYNCING", "last_sync": "2026-08-04T12:00:00Z", "account_count": 4},
        {"id": "inst-ing", "name": "ING", "country": "NL", "status": "ERROR", "last_sync": None, "account_count": 1},
        {"id": "inst-caixa", "name": "CaixaBank", "country": "ES", "status": "ACTIVE", "last_sync": "2026-08-04T15:00:00Z", "account_count": 2},
    ]
