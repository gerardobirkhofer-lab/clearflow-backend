import uuid  from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, case
from datetime import datetime, timedelta
from typing import Optional

from app.core.database import get_db
from app.models.dispute import Dispute
from app.models.bank_transaction import BankTransaction

router = APIRouter()

@router.get("/")
async def list_disputes(
    tenant_id: uuid.UUID,
    status: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    days_min: Optional[int] = Query(None),
    days_max: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    query = select(Dispute).where(Dispute.tenant_id == tenant_id)
    
    if status:
        query = query.where(Dispute.status == status)
    if provider:
        query = query.where(Dispute.provider_name == provider)
    if days_min is not None:
        cutoff = datetime.utcnow() - timedelta(days=days_min)
        query = query.where(Dispute.opened_at <= cutoff)
    if days_max is not None:
        cutoff = datetime.utcnow() - timedelta(days=days_max)
        query = query.where(Dispute.opened_at >= cutoff)
    
    result = await db.execute(query.order_by(Dispute.opened_at.desc()))
    disputes = result.scalars().all()
    
    return {
        "disputes": [
            {
                "id": d.id,
                "provider_name": d.provider_name,
                "dispute_type": d.dispute_type,
                "status": d.status,
                "amount": d.amount,
                "description": d.description,
                "opened_at": d.opened_at.isoformat() if d.opened_at else None,
                "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
                "expected_resolution": d.expected_resolution.isoformat() if d.expected_resolution else None,
                "days_to_resolve": d.days_to_resolve,
                "recovery_amount": d.recovery_amount,
                "age_days": (datetime.utcnow() - d.opened_at).days if d.opened_at else 0,
            }
            for d in disputes
        ],
        "total": len(disputes),
    }

@router.get("/summary")
async def get_dispute_summary(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Total disputes
    total_result = await db.execute(
        select(func.count(Dispute.id)).where(Dispute.tenant_id == tenant_id)
    )
    total = total_result.scalar() or 0
    
    # By status
    status_result = await db.execute(
        select(Dispute.status, func.count().label("count"), func.sum(Dispute.amount).label("total"))
        .where(Dispute.tenant_id == tenant_id)
        .group_by(Dispute.status)
    )
    by_status = {row.status: {"count": row.count, "amount": row.total or 0} for row in status_result}
    
    # By provider
    provider_result = await db.execute(
        select(Dispute.provider_name, func.count().label("count"), func.sum(Dispute.amount).label("total"))
        .where(Dispute.tenant_id == tenant_id)
        .group_by(Dispute.provider_name)
    )
    by_provider = {row.provider_name: {"count": row.count, "amount": row.total or 0} for row in provider_result}
    
    # Aging buckets
    now = datetime.utcnow()
    aging = {
        "0_7": 0,
        "8_15": 0,
        "16_30": 0,
        "31_45": 0,
        "46_plus": 0,
    }
    aging_amounts = {
        "0_7": 0.0,
        "8_15": 0.0,
        "16_30": 0.0,
        "31_45": 0.0,
        "46_plus": 0.0,
    }
    
    open_disputes = await db.execute(
        select(Dispute).where(
            and_(Dispute.tenant_id == tenant_id, Dispute.status == "open")
        )
    )
    for d in open_disputes.scalars().all():
        age = (now - d.opened_at).days if d.opened_at else 0
        if age <= 7:
            aging["0_7"] += 1
            aging_amounts["0_7"] += d.amount or 0
        elif age <= 15:
            aging["8_15"] += 1
            aging_amounts["8_15"] += d.amount or 0
        elif age <= 30:
            aging["16_30"] += 1
            aging_amounts["16_30"] += d.amount or 0
        elif age <= 45:
            aging["31_45"] += 1
            aging_amounts["31_45"] += d.amount or 0
        else:
            aging["46_plus"] += 1
            aging_amounts["46_plus"] += d.amount or 0
    
    # Resolution stats
    resolved = await db.execute(
        select(Dispute).where(
            and_(Dispute.tenant_id == tenant_id, Dispute.status == "resolved")
        )
    )
    resolved_disputes = resolved.scalars().all()
    avg_days = sum(d.days_to_resolve or 0 for d in resolved_disputes) / len(resolved_disputes) if resolved_disputes else 0
    total_recovered = sum(d.recovery_amount or 0 for d in resolved_disputes)
    
    # Weekly trend (last 8 weeks)
    weeks = []
    for i in range(7, -1, -1):
        week_start = now - timedelta(days=now.weekday() + (i * 7))
        week_end = week_start + timedelta(days=7)
        
        opened_week = await db.execute(
            select(func.count(Dispute.id)).where(
                and_(
                    Dispute.tenant_id == tenant_id,
                    Dispute.opened_at >= week_start,
                    Dispute.opened_at < week_end,
                )
            )
        )
        resolved_week = await db.execute(
            select(func.count(Dispute.id)).where(
                and_(
                    Dispute.tenant_id == tenant_id,
                    Dispute.resolved_at >= week_start,
                    Dispute.resolved_at < week_end,
                )
            )
        )
        weeks.append({
            "week": week_start.strftime("%d %b"),
            "opened": opened_week.scalar() or 0,
            "resolved": resolved_week.scalar() or 0,
        })
    
    return {
        "total_disputes": total,
        "by_status": by_status,
        "by_provider": by_provider,
        "aging": aging,
        "aging_amounts": aging_amounts,
        "avg_resolution_days": round(avg_days, 1),
        "total_recovered": total_recovered,
        "open_amount": by_status.get("open", {}).get("amount", 0),
        "weekly_trend": weeks,
    }

@router.post("/", status_code=201)
async def create_dispute(
    tenant_id: uuid.UUID,
    provider_name: str,
    dispute_type: str,
    amount: float,
    description: str = "",
    expected_resolution_days: int = 14,
    db: AsyncSession = Depends(get_db)
):
    dispute = Dispute(
        tenant_id=tenant_id,
        provider_name=provider_name,
        dispute_type=dispute_type,
        status="open",
        amount=amount,
        description=description,
        expected_resolution=datetime.utcnow() + timedelta(days=expected_resolution_days),
    )
    db.add(dispute)
    await db.commit()
    await db.refresh(dispute)
    return {"id": dispute.id, "message": "Dispute created"}

@router.patch("/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: int,
    recovery_amount: float,
    notes: str = "",
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    
    dispute.status = "resolved"
    dispute.resolved_at = datetime.utcnow()
    dispute.days_to_resolve = (dispute.resolved_at - dispute.opened_at).days if dispute.opened_at else 0
    dispute.recovery_amount = recovery_amount
    dispute.resolution_notes = notes
    
    await db.commit()
    return {"message": "Dispute resolved", "days_to_resolve": dispute.days_to_resolve}
