"""Each place keeps its own sales, and a bill lands on the day the client set."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.open_matching import madrid_today  # noqa: E402


def _owner(client: TestClient):
    suffix = uuid.uuid4().hex[:8]
    owner = client.post(
        "/api/v1/auth/register",
        json={"email": f"panel-{suffix}@example.com", "password": "panel-password", "full_name": "Panel"},
    )
    assert owner.status_code == 201, owner.text
    body = owner.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


def _site(client, tenant_id, headers, name):
    created = client.post(
        f"/api/v1/companies/{tenant_id}/sites",
        headers=headers,
        json={"name": name, "kind": "restaurant"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_sales_stay_on_their_place_and_a_bill_lands_on_its_day():
    today = madrid_today()
    with TestClient(app) as client:
        tenant_id, headers = _owner(client)
        first = _site(client, tenant_id, headers, "Local 1")
        second = _site(client, tenant_id, headers, "Local 2")

        sale = client.post(
            f"/api/v1/companies/{tenant_id}/sites/{first}/sales",
            headers=headers,
            json={"amount": "100,50", "sold_on": "2026-10-01", "provider": "Stripe", "concept": "turno"},
        )
        assert sale.status_code == 201, sale.text

        contract = client.post(
            f"/api/v1/companies/{tenant_id}/contracts",
            headers=headers,
            data={"provider_name": "Stripe", "fee_percent": "10", "payout_days": "0"},
        )
        assert contract.status_code == 200, contract.text

        due = client.post(
            "/api/v1/expenses",
            headers=headers,
            params={"tenant_id": tenant_id},
            json={"kind": "supplier", "concept": "Pescado", "amount": "200", "due_day": today.day, "site_id": first},
        )
        assert due.status_code == 200, due.text
        assert due.json()["due_day"] == today.day

        panel = client.get("/api/v1/panel", headers=headers)
        assert panel.status_code == 200, panel.text
        places = {item["name"]: item for item in panel.json()["places"]}
        assert places["Local 1"]["sales"] == 100.5
        assert places["Local 1"]["unresolved"] == 100.5
        assert places["Local 1"]["this_check"] == 100.5
        assert places["Local 1"]["uncollected_count"] == 1
        assert places["Local 2"]["sales"] == 0
        assert panel.json()["unresolved_total"] == 100.5
        assert places["Local 1"]["contract_fees"] == 10.05
        assert places["Local 1"]["earning"] == round(100.5 - 10.05 - 200, 2)
        assert places["Local 1"]["verdict"] == "vamos"

        caja = client.get("/api/v1/caja", headers=headers)
        assert caja.status_code == 200, caja.text
        local = next(item for item in caja.json()["places"] if item["name"] == "Local 1")
        today_row = local["days"][0]
        assert today_row["date"] == today.isoformat()
        assert today_row["outflows"] == 200
        assert today_row["bills"][0]["concept"] == "Pescado"
        other = next(item for item in caja.json()["places"] if item["name"] == "Local 2")
        assert other["days"][0]["outflows"] == 0

        sold = (today - timedelta(days=0)).isoformat()
        landed = client.post(
            f"/api/v1/companies/{tenant_id}/sites/{second}/sales",
            headers=headers,
            json={"amount": "50", "sold_on": sold, "provider": "Stripe", "concept": "noche"},
        )
        assert landed.status_code == 201, landed.text
        again = client.get("/api/v1/caja", headers=headers)
        second_place = next(item for item in again.json()["places"] if item["name"] == "Local 2")
        assert second_place["days"][0]["inflows"] == 45

        closed = client.patch(
            f"/api/v1/companies/{tenant_id}/sites/{second}",
            headers=headers,
            json={"active": False},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["active"] is False
        after = client.get("/api/v1/panel", headers=headers)
        names = [item["name"] for item in after.json()["places"]]
        assert "Local 2" not in names
        assert "Local 1" in names
