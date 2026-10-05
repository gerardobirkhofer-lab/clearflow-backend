"""A confirmed contract sets the fee. The open amount is tried against that net."""
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

DB = "postgresql://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test"
CONTRACT = """CONTRATO DE TPV
La comisión aplicable es del 1,40% más 0,20 euros por operación.
El abono se realiza a los 2 días.
"""
PROVIDER_CSV = """Fecha;Concepto;Importe
06/08/2026;VENTA TARJETA;100,00
"""
BANK_OK = """Fecha;Concepto;Importe;Saldo
08/08/2026;LIQUIDACION;98,40;98,40
"""
BANK_SHORT = """Fecha;Concepto;Importe;Saldo
08/08/2026;LIQUIDACION CORTA;97,00;97,00
"""


def _query(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_contract_fee_is_saved_and_used_on_the_open_amount():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"contrato-{suffix}@example.com", "password": "contrato-password", "full_name": "Contrato"},
        )
        assert registered.status_code == 201, registered.text
        tenant_id = registered.json()["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}

        other = client.post(
            "/api/v1/auth/register",
            json={"email": f"ajeno-{suffix}@example.com", "password": "ajeno-password", "full_name": "Ajeno"},
        )
        assert other.status_code == 201, other.text
        denied = client.post(
            f"/api/v1/companies/{tenant_id}/contracts",
            headers={"Authorization": f"Bearer {other.json()['token']}"},
            data={"provider_name": "TPV", "fee_percent": "9"},
        )
        assert denied.status_code == 403, denied.text

        read = client.post(
            f"/api/v1/companies/{tenant_id}/contracts/read",
            headers=headers,
            files={"file": ("tpv.txt", io.BytesIO(CONTRACT.encode()), "text/plain")},
        )
        assert read.status_code == 200, read.text
        proposal = read.json()
        assert proposal["fee_percent"] == 1.4
        assert proposal["fee_fixed"] == 0.2
        assert proposal["payout_days"] == 2
        assert "1,40%" not in str(proposal)

        saved = client.post(
            f"/api/v1/companies/{tenant_id}/contracts",
            headers=headers,
            data={
                "provider_name": "TPV",
                "fee_percent": str(proposal["fee_percent"]),
                "fee_fixed": str(proposal["fee_fixed"]),
                "payout_days": str(proposal["payout_days"]),
            },
            files={"file": ("tpv.txt", io.BytesIO(CONTRACT.encode()), "text/plain")},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["fee_percent"] == 1.4
        assert "1,40%" not in saved.text

        listed = client.get(f"/api/v1/companies/{tenant_id}/contracts", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["provider_name"] == "TPV"
        assert listed.json()["items"][0]["payout_days"] == 2

        stored = _query(
            "SELECT contract_body FROM providers WHERE tenant_id = $1 AND terms_confirmed = 1",
            uuid.UUID(tenant_id),
        )
        assert stored[0]["contract_body"].startswith("enc:v1:")
        assert "1,40%" in decrypt_secret(stored[0]["contract_body"])

        provider = client.post(
            "/api/v1/providers/upload",
            headers=headers,
            data={"tenant_id": tenant_id, "provider_name": "TPV"},
            files={"file": ("tpv.csv", io.BytesIO(PROVIDER_CSV.encode()), "text/csv")},
        )
        assert provider.status_code == 200, provider.text

        short = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": tenant_id},
            files={"file": ("corto.csv", io.BytesIO(BANK_SHORT.encode()), "text/csv")},
        )
        assert short.status_code == 200, short.text
        status = client.get("/api/v1/reconciliation/status", headers=headers, params={"tenant_id": tenant_id})
        assert status.json()["summary"]["matched_count"] == 0
        open_line = status.json()["unmatched_provider"][0]
        assert open_line["expected_net"] == 98.4
        assert open_line["expected_date"] == "2026-08-08"

        bank = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": tenant_id},
            files={"file": ("santander.csv", io.BytesIO(BANK_OK.encode()), "text/csv")},
        )
        assert bank.status_code == 200, bank.text
        done = client.get("/api/v1/reconciliation/status", headers=headers, params={"tenant_id": tenant_id})
        body = done.json()
        assert body["summary"]["matched_count"] == 1
        assert body["summary"]["matched_amount"] == 98.4
        assert body["matched"][0]["settlement"]["fee_difference"] == 0
        assert body["matched"][0]["settlement"]["days_late"] == 0
