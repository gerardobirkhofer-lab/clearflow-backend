"""
Persisted reconciliation jobs (Postgres via SharedSessionLocal).

Survives Railway restarts. In-memory fallback only if DB write fails at create time.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.database import SharedSessionLocal
from app.models.recon_job import ReconJob

_fallback: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _row_to_dict(row: ReconJob) -> dict[str, Any]:
    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except Exception:
            result = {"raw": row.result_json[:500]}
    return {
        "id": row.id,
        "tenant_id": str(row.tenant_id),
        "status": row.status,
        "progress": row.progress or 0,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "result": result,
        "error": row.error,
    }


async def create_job(tenant_id: uuid.UUID) -> str:
    job_id = str(uuid.uuid4())
    try:
        async with SharedSessionLocal() as db:
            db.add(
                ReconJob(
                    id=job_id,
                    tenant_id=tenant_id,
                    status="queued",
                    progress=0,
                )
            )
            await db.commit()
        return job_id
    except Exception:
        _fallback[job_id] = {
            "id": job_id,
            "tenant_id": str(tenant_id),
            "status": "queued",
            "progress": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "result": None,
            "error": None,
            "_fallback": True,
        }
        return job_id


async def update_job(job_id: str, **fields: Any) -> None:
    if job_id in _fallback:
        job = _fallback[job_id]
        if "result" in fields:
            job["result"] = fields.pop("result")
        job.update(fields)
        job["updated_at"] = _now()
        return

    result = fields.pop("result", None)
    try:
        async with SharedSessionLocal() as db:
            row = await db.get(ReconJob, job_id)
            if not row:
                return
            if "status" in fields:
                row.status = fields["status"]
            if "progress" in fields:
                row.progress = int(fields["progress"])
            if "error" in fields:
                row.error = fields["error"]
            if result is not None:
                row.result_json = json.dumps(result, ensure_ascii=False)
            row.updated_at = datetime.utcnow()
            await db.commit()
    except Exception:
        # best-effort; poller may see stale progress
        pass


async def get_job(job_id: str) -> dict[str, Any] | None:
    if job_id in _fallback:
        return dict(_fallback[job_id])
    try:
        async with SharedSessionLocal() as db:
            row = await db.get(ReconJob, job_id)
            if not row:
                return None
            return _row_to_dict(row)
    except Exception:
        return None


async def list_jobs_for_tenant(tenant_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    try:
        async with SharedSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ReconJob)
                    .where(ReconJob.tenant_id == tenant_id)
                    .order_by(ReconJob.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [_row_to_dict(r) for r in rows]
    except Exception:
        return [
            dict(j)
            for j in _fallback.values()
            if j.get("tenant_id") == str(tenant_id)
        ][:limit]
