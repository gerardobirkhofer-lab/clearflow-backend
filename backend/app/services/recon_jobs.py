"""
In-process async reconciliation jobs (no Celery tasks registered yet).

Fine for single-instance Railway. Jobs are lost on restart — acceptable for pilot.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

_jobs: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


async def create_job(tenant_id: uuid.UUID) -> str:
    job_id = str(uuid.uuid4())
    async with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "tenant_id": str(tenant_id),
            "status": "queued",
            "progress": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "result": None,
            "error": None,
        }
    return job_id


async def update_job(job_id: str, **fields: Any) -> None:
    async with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = _now()


async def get_job(job_id: str) -> dict[str, Any] | None:
    async with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
