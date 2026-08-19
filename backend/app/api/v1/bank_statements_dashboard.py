import uuid
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.bank_transaction import BankTransaction
from app.models.provider_transaction import ProviderTransaction

router = APIRouter()

@router.get("/dashboard")
async def get_dashboard(
    tenant_id: Optional[uuid.UUID] = None,
    client_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db)
):
    # --- Resolve tenant ids ---
    tenant_ids: List[uuid.UUID] = []
    tenant_map = {}  # id -> name
    
    if client_id:
        # Assumes your Tenant model has client_id. 
        # If you haven't added it yet, this will need the model update first.
        from app.models_orm import Tenant
        result = await db.execute(select(Tenant).where(Tenant.client_id == client_id))
        tenants = result.scalars().all()
        tenant_ids = [t.id for t in tenants]
        tenant_map = {t.id: {"name": t.name, "type": getattr(t, 'type', 'store')} for t in tenants}
        if not tenant_ids:
            return _empty_response()
    elif tenant_id:
        tenant_ids = [tenant_id]
        from app.models_orm import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        t = t_result.scalar_one_or_none()
        if t:
            tenant_map = {t.id: {"name": t.name, "type": getattr(t, 'type', 'store')}}
    else:
        raise HTTPException(status_code=400, detail="Provide tenant_id or client_id")

    # --- Fetch transactions ---
    bank_result = await db.execute(
        select(BankTransaction).where(BankTransaction.tenant_id.in_(tenant_ids))
    )
    bank_txs = bank_result.scalars().all()
    
    prov_result = await db.execute(
        select(ProviderTransaction).where(ProviderTransaction.tenant_id.in_(tenant_ids))
    )
    prov_txs = prov_result.scalars().all()

    # --- Single store: return classic view ---
    if len(tenant_ids) == 1:
        return _build_single_view(bank_txs, prov_txs, tenant_id)

    # --- Portfolio: aggregate + per-store breakdown ---
    stores_data = []
    total_sales = 0.0
    total_collected = 0.0
    total_matched = 0.0
    total_missing = 0.0

    for tid in tenant_ids:
        b_txs = [b for b in bank_txs if b.tenant_id == tid]
        p_txs = [p for p in prov_txs if p.tenant_id == tid]
        
        s_collected = sum(b.amount for b in b_txs)
        s_sales = sum(p.amount for p in p_txs)
        s_matched = sum(b.amount for b in b_txs if b.matched)
        s_missing = abs(sum(p.amount for p in p_txs if not p.matched)) + abs(sum(b.amount for b in b_txs if not b.matched))
        s_rate = (s_matched / s_sales * 100) if s_sales else 0
        
        stores_data.append({
            "id": tid,
            "name": tenant_map.get(tid, {}).get("name", f"Store {tid}"),
            "type": tenant_map.get(tid, {}).get("type", "store"),
            "total_sales": s_sales,
            "total_collected": s_collected,
            "matched_amount": s_matched,
            "missing_amount": s_missing,
            "collection_rate": s_rate,
            "pending_count": sum(1 for b in b_txs if not b.matched) + sum(1 for p in p_txs if not p.matched)
        })
        
        total_sales += s_sales
        total_collected += s_collected
        total_matched += s_matched
        total_missing += s_missing

    # --- Recent activity across all stores ---
    recent_bank = _serialize_recent(bank_txs, "bank")
    recent_provider = _serialize_recent(prov_txs, "provider")
    all_recent = sorted(recent_bank + recent_provider, key=lambda x: x["date"] or "", reverse=True)[:10]

    return {
        "view": "portfolio",
        "summary": {
            "total_collected": total_collected,
            "total_sales": total_sales,
            "matched_amount": total_matched,
            "missing_amount": total_missing,
            "collection_rate": (total_matched / total_sales * 100) if total_sales else 0,
            "bank_count": len(bank_txs),
            "provider_count": len(prov_txs),
            "matched_count": sum(1 for b in bank_txs if b.matched),
            "pending_count": sum(1 for b in bank_txs if not b.matched) + sum(1 for p in prov_txs if not p.matched),
        },
        "stores": sorted(stores_data, key=lambda x: x["total_sales"], reverse=True),
        "recent_activity": all_recent,
        "discrepancies": {
            "unmatched_bank": _serialize_unmatched([b for b in bank_txs if not b.matched]),
            "unmatched_provider": _serialize_unmatched([p for p in prov_txs if not p.matched], provider=True),
        }
    }


def _build_single_view(bank_txs, prov_txs, tenant_id):
    total_bank = sum(b.amount for b in bank_txs)
    total_provider = sum(p.amount for p in prov_txs)
    matched_bank = sum(b.amount for b in bank_txs if b.matched)
    pending_bank = sum(b.amount for b in bank_txs if not b.matched)
    pending_provider = sum(p.amount for p in prov_txs if not p.matched)
    
    recent_bank = _serialize_recent(bank_txs, "bank")
    recent_provider = _serialize_recent(prov_txs, "provider")
    
    return {
        "view": "store",
        "summary": {
            "total_collected": total_bank,
            "total_sales": total_provider,
            "matched_amount": matched_bank,
            "missing_amount": abs(pending_provider) + abs(pending_bank),
            "collection_rate": (matched_bank / total_provider * 100) if total_provider else 0,
            "bank_count": len(bank_txs),
            "provider_count": len(prov_txs),
            "matched_count": sum(1 for b in bank_txs if b.matched),
            "pending_count": sum(1 for b in bank_txs if not b.matched) + sum(1 for p in prov_txs if not p.matched),
        },
        "recent_activity": sorted(recent_bank + recent_provider, key=lambda x: x["date"] or "", reverse=True)[:10],
        "discrepancies": {
            "unmatched_bank": _serialize_unmatched([b for b in bank_txs if not b.matched]),
            "unmatched_provider": _serialize_unmatched([p for p in prov_txs if not p.matched], provider=True),
        }
    }

def _empty_response():
    return {
        "view": "portfolio",
        "summary": {"total_collected":0,"total_sales":0,"matched_amount":0,"missing_amount":0,"collection_rate":0,"bank_count":0,"provider_count":0,"matched_count":0,"pending_count":0},
        "stores": [],
        "recent_activity": [],
        "discrepancies": {"unmatched_bank":[],"unmatched_provider":[]}
    }

def _serialize_recent(txs, tx_type):
    out = []
    for t in txs:
        item = {
            "id": t.id,
            "concept": getattr(t, 'concept', None),
            "amount": t.amount,
            "date": t.transaction_date.isoformat() if getattr(t, 'transaction_date', None) else None,
            "matched": getattr(t, 'matched', False),
            "type": tx_type,
        }
        if tx_type == "provider":
            item["provider_name"] = getattr(t, 'provider_name', 'Provider')
        out.append(item)
    return out

def _serialize_unmatched(txs, provider=False):
    out = []
    for t in txs:
        item = {
            "id": t.id,
            "concept": getattr(t, 'concept', None),
            "amount": t.amount,
            "date": t.transaction_date.isoformat() if getattr(t, 'transaction_date', None) else None,
        }
        if provider:
            item["provider_name"] = getattr(t, 'provider_name', 'Provider')
        out.append(item)
    return out
