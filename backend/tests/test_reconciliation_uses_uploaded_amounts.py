"""Uploaded CSV amounts are what reconciliation reports. No sample totals."""
from __future__ import annotations

import io
import os
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BANK_CSV = """Fecha;Concepto;Importe;Saldo
06/08/2026;TRANSFERENCIA RECIBIDA CLIENTE A;+1234,56;15234,56
06/08/2026;COMISION TPV;-12,50;15222,06
06/08/2026;STRIPE PAYOUT;+890,00;16112,06
06/08/2026;TRANSFERENCIA RECIBIDA CLIENTE B;+567,89;16679,95
06/08/2026;TARJETA COMPRA SUPERMERCADO;-45,30;16634,65
"""

PROVIDER_CSV = """Fecha;Concepto;Importe
06/08/2026;STRIPE PAYOUT;890,00
"""


def test_upload_and_reconcile_reports_file_amounts():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"books-{suffix}@example.com",
                "password": "books-password",
                "full_name": "Books",
            },
        )
        assert registered.status_code == 201
        body = registered.json()
        tenant_id = body["user"]["tenant_id"]
        headers = {"Authorization": f"Bearer {body['token']}"}

        bank = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": tenant_id},
            files={"file": ("santander.csv", io.BytesIO(BANK_CSV.encode()), "text/csv")},
        )
        assert bank.status_code == 200, bank.text
        assert bank.json()["count"] == 5

        provider = client.post(
            "/api/v1/providers/upload",
            headers=headers,
            data={"tenant_id": tenant_id, "provider_name": "Stripe"},
            files={"file": ("stripe.csv", io.BytesIO(PROVIDER_CSV.encode()), "text/csv")},
        )
        assert provider.status_code == 200, provider.text
        assert provider.json()["count"] == 1

        run = client.post(
            "/api/v1/reconciliation/run",
            headers=headers,
            params={"tenant_id": tenant_id},
        )
        assert run.status_code == 200, run.text
        payload = run.json()
        summary = payload["summary"]
        assert summary["total_bank"] == 5
        assert summary["total_provider"] == 1
        assert summary["matched_count"] == 1
        assert summary["unmatched_bank_count"] == 4
        assert summary["unmatched_provider_count"] == 0
        assert summary["matched_amount"] == 890.0
        assert payload["matched_amount"] == 890.0
        assert payload["matched"][0]["bank"]["amount"] == 890.0
        assert payload["matched"][0]["provider"]["provider_name"] == "Stripe"
        assert 12450.75 not in payload.values()

        status = client.get(
            "/api/v1/reconciliation/status",
            headers=headers,
            params={"tenant_id": tenant_id},
        )
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["summary"]["matched_count"] == 1
        assert status_body["bank_transactions"] == 5
        assert status_body["pending_bank"] == 4
        concepts = {row["concept"] for row in status_body["unmatched_bank"]}
        assert "STRIPE PAYOUT" not in concepts
        assert "TRANSFERENCIA RECIBIDA CLIENTE A" in concepts

        dashboard = client.get(
            "/api/v1/bank-statements/dashboard",
            headers=headers,
            params={"tenant_id": tenant_id},
        )
        assert dashboard.status_code == 200, dashboard.text
        dash_summary = dashboard.json()["summary"]
        assert dash_summary["bank_transactions"] == 5
        assert dash_summary["matched_count"] == 1
        assert round(dash_summary["matched_amount"], 2) == 890.0
        assert dash_summary["pending_count"] == 4
