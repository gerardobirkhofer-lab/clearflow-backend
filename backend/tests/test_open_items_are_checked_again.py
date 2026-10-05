"""Yesterday's finished match stays. An open amount is tried again when the other side arrives."""
from __future__ import annotations

import asyncio
import io
import os
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.open_matching import madrid_today  # noqa: E402

DB = "postgresql://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test"
PROVIDER_CSV = """Fecha;Concepto;Importe
06/08/2026;STRIPE PAYOUT;890,00
"""
BANK_CSV = """Fecha;Concepto;Importe;Saldo
06/08/2026;STRIPE PAYOUT;+890,00;890,00
"""


def _query(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_open_amount_is_matched_the_next_day_and_yesterday_stays():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"open-{suffix}@example.com", "password": "open-password", "full_name": "Abierta"},
        )
        assert registered.status_code == 201, registered.text
        tenant_id = registered.json()["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}

        provider = client.post(
            "/api/v1/providers/upload",
            headers=headers,
            data={"tenant_id": tenant_id, "provider_name": "Stripe"},
            files={"file": ("stripe.csv", io.BytesIO(PROVIDER_CSV.encode()), "text/csv")},
        )
        assert provider.status_code == 200, provider.text

        first = client.post("/api/v1/reconciliation/run", headers=headers, params={"tenant_id": tenant_id})
        assert first.status_code == 200, first.text
        assert first.json()["summary"]["matched_count"] == 0
        assert first.json()["summary"]["unmatched_provider_count"] == 1

        yesterday = madrid_today() - timedelta(days=1)
        _query(
            "UPDATE reconciliation_days SET day = $2 WHERE tenant_id = $1",
            uuid.UUID(tenant_id),
            yesterday,
        )

        bank = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": tenant_id},
            files={"file": ("santander.csv", io.BytesIO(BANK_CSV.encode()), "text/csv")},
        )
        assert bank.status_code == 200, bank.text

        days = client.get("/api/v1/reconciliation/days", headers=headers, params={"tenant_id": tenant_id})
        assert days.status_code == 200, days.text
        by_day = {item["day"]: item for item in days.json()["items"]}
        assert by_day[yesterday.isoformat()]["matched_count"] == 0
        assert by_day[yesterday.isoformat()]["open_provider_count"] == 1
        assert by_day[madrid_today().isoformat()]["matched_count"] == 1
        assert by_day[madrid_today().isoformat()]["open_provider_count"] == 0
        assert by_day[madrid_today().isoformat()]["matched_amount"] == 890.0

        again = client.post("/api/v1/reconciliation/run", headers=headers, params={"tenant_id": tenant_id})
        assert again.json()["summary"]["matched_count"] == 1
        assert again.json()["summary"]["matched_amount"] == 890.0
