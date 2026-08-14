from fastapi import APIRouter
from . import auth, tenants, bank_statements, providers, reconciliation, dashboard, stripe_connect

router = APIRouter()
router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
router.include_router(bank_statements.router, prefix="/bank-statements", tags=["bank-statements"])
router.include_router(providers.router, prefix="/providers", tags=["providers"])
router.include_router(reconciliation.router, prefix="/reconciliation", tags=["reconciliation"])
router.include_router(dashboard.router, prefix="/bank-statements", tags=["bank-statements"])
router.include_router(stripe_connect.router, prefix="/stripe", tags=["stripe"])
