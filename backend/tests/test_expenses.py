"""Monthly costs change what is left, and a later check shows what finally arrived."""
from __future__ import annotations

import asyncio
import io
import os
import uuid
from datetime import timedelta

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


def test_expenses_are_private_and_reduce_what_is_left():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"gastos-{suffix}@example.com", "password": "gastos-password", "full_name": "Gastos"},
        )
        assert owner.status_code == 201, owner.text
        tenant_id = owner.json()["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {owner.json()['token']}"}

        other = client.post(
            "/api/v1/auth/register",
            json={"email": f"ajeno-gastos-{suffix}@example.com", "password": "ajeno-password", "full_name": "Ajeno"},
        )
        assert other.status_code == 201, other.text
        other_headers = {"Authorization": f"Bearer {other.json()['token']}"}

        rejected = client.post(
            "/api/v1/expenses",
            headers=headers,
            params={"tenant_id": tenant_id},
            json={"kind": "rent", "concept": "Local", "amount": 0},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "El importe tiene que ser mayor que cero."

        rent = client.post(
            "/api/v1/expenses",
            headers=headers,
            params={"tenant_id": tenant_id},
            json={"kind": "rent", "concept": "Local", "amount": "1.200,50"},
        )
        assert rent.status_code == 200, rent.text
        assert rent.json()["amount"] == 1200.5
        assert rent.json()["kind_label"] == "Alquiler"

        wages = client.post(
            "/api/v1/expenses",
            headers=headers,
            params={"tenant_id": tenant_id},
            json={"kind": "wage", "concept": "Turno de sala", "amount": 800},
        )
        assert wages.status_code == 200, wages.text

        hidden = client.get("/api/v1/expenses", headers=other_headers, params={"tenant_id": tenant_id})
        assert hidden.status_code == 403

        listed = client.get("/api/v1/expenses", headers=headers, params={"tenant_id": tenant_id})
        assert listed.status_code == 200
        assert [item["concept"] for item in listed.json()["items"]] == ["Local", "Turno de sala"]

        outlook = client.get("/api/v1/expenses/outlook", headers=headers, params={"tenant_id": tenant_id})
        assert outlook.status_code == 200, outlook.text
        body = outlook.json()
        assert body["has_expenses"] is True
        assert body["expenses_monthly"] == 2000.5
        assert body["waiting_amount"] == 0
        assert body["left_after_expenses"] == -2000.5

        removed = client.delete(
            f"/api/v1/expenses/{wages.json()['id']}",
            headers=other_headers,
            params={"tenant_id": other.json()["user"]["tenant_id"]},
        )
        assert removed.status_code == 404

        gone = client.delete(
            f"/api/v1/expenses/{wages.json()['id']}",
            headers=headers,
            params={"tenant_id": tenant_id},
        )
        assert gone.status_code == 200
        left = client.get("/api/v1/expenses/outlook", headers=headers, params={"tenant_id": tenant_id})
        assert left.json()["expenses_monthly"] == 1200.5


def test_money_that_was_open_shows_up_on_the_next_check():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"outlook-{suffix}@example.com", "password": "outlook-password", "full_name": "Outlook"},
        )
        assert registered.status_code == 201, registered.text
        tenant_id = registered.json()["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}

        client.post(
            "/api/v1/expenses",
            headers=headers,
            params={"tenant_id": tenant_id},
            json={"kind": "salary", "concept": "Equipo", "amount": 500},
        )
        provider = client.post(
            "/api/v1/providers/upload",
            headers=headers,
            data={"tenant_id": tenant_id, "provider_name": "Stripe"},
            files={"file": ("stripe.csv", io.BytesIO(PROVIDER_CSV.encode()), "text/csv")},
        )
        assert provider.status_code == 200, provider.text

        first = client.get("/api/v1/expenses/outlook", headers=headers, params={"tenant_id": tenant_id})
        assert first.status_code == 200, first.text
        assert first.json()["waiting_amount"] == 890.0
        assert first.json()["left_after_expenses"] == 390.0

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

        done = client.get("/api/v1/expenses/outlook", headers=headers, params={"tenant_id": tenant_id})
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["waiting_amount"] == 0
        assert body["matched_amount"] == 890.0
        assert body["arrived_since_previous_amount"] == 890.0
        assert body["arrived_since_previous_count"] == 1
        assert body["previous_open_count"] == 1
        assert body["left_after_expenses"] == -500.0
        assert body["previous_day"] == yesterday.isoformat()
