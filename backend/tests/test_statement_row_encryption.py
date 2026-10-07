"""The original uploaded row is encrypted. The screen still receives the amount."""
from __future__ import annotations

import asyncio
import io
import os
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.secrets import decrypt_secret  # noqa: E402
from app.main import app  # noqa: E402

IBAN = "ES7621000418401234567891"
DB = "postgresql://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test"
BANK_CSV = f"""Fecha;Concepto;Importe;IBAN
06/08/2026;COBRO LOCAL;10,00;{IBAN}
"""


def _query(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_uploaded_statement_hides_the_original_row():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"row-{suffix}@example.com", "password": "row-password", "full_name": "Fila"},
        )
        assert registered.status_code == 201, registered.text
        tenant_id = registered.json()["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}
        uploaded = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": tenant_id},
            files={"file": ("santander.csv", io.BytesIO(BANK_CSV.encode()), "text/csv")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["count"] == 1
        assert IBAN not in uploaded.text

        listed = client.get(
            "/api/v1/bank-statements/",
            headers=headers,
            params={"tenant_id": tenant_id},
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["transactions"][0]["amount"] == 10.0
        assert listed.json()["transactions"][0]["concept"] == "COBRO LOCAL"
        assert IBAN not in listed.text

        stored = _query(
            "SELECT raw_data FROM bank_transactions WHERE tenant_id = $1",
            uuid.UUID(tenant_id),
        )
        assert stored[0]["raw_data"].startswith("enc:v1:")
        assert IBAN in decrypt_secret(stored[0]["raw_data"])
