"""
API Router: Morning Reports
Generates and retrieves daily morning reports.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ...core.auth import get_current_user, CurrentUser
from ...core.tenant import get_current_tenant
from ...models_orm import MorningReport
from ...schemas import MorningReportResponse, MorningReportListResponse
from ...services.report_service import ReportService

router = APIRouter(prefix="/morning-reports")


@router.get("/latest", response_model=MorningReportResponse)
async def get_latest_report(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get the most recent morning report for this tenant."""
    query = select(MorningReport).where(
        MorningReport.tenant_id == tenant_id
    ).order_by(MorningReport.report_date.desc()).limit(1)

    result = await db.execute(query)
    report = result.scalar_one_or_none()

    if not report:
        # Generate on-the-fly if none exists
        service = ReportService(db, tenant_id)
        report = await service.generate_morning_report(date.today() - timedelta(days=1))

    return report


@router.get("/{report_date}", response_model=MorningReportResponse)
async def get_report_by_date(
    report_date: date,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Get morning report for a specific date."""
    query = select(MorningReport).where(
        and_(MorningReport.tenant_id == tenant_id, MorningReport.report_date == report_date)
    )

    result = await db.execute(query)
    report = result.scalar_one_or_none()

    if not report:
        raise HTTPException(status_code=404, detail="Report not found for this date")

    return report


@router.get("", response_model=MorningReportListResponse)
async def list_reports(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """List historical morning reports."""
    query = select(MorningReport).where(MorningReport.tenant_id == tenant_id)

    if start_date:
        query = query.where(MorningReport.report_date >= start_date)
    if end_date:
        query = query.where(MorningReport.report_date <= end_date)

    query = query.order_by(MorningReport.report_date.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    reports = result.scalars().all()

    return MorningReportListResponse(
        items=reports,
        page=page,
        page_size=page_size,
    )


@router.post("/generate/{report_date}", response_model=MorningReportResponse)
async def generate_report(
    report_date: date,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    tenant_id: UUID = Depends(get_current_tenant),
):
    """Manually trigger report generation for a specific date."""
    service = ReportService(db, tenant_id)
    report = await service.generate_morning_report(report_date)
    return report
