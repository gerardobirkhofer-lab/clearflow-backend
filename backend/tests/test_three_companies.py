"""One owner can open three companies. A manager can open only the one they were given."""
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


def _csv(concept: str, amount: str) -> bytes:
    text = f"Fecha;Concepto;Importe;Saldo\n06/08/2026;{concept};{amount};100,00\n"
    return text.encode()


def _concepts(payload: dict) -> set[str]:
    rows = payload.get("unmatched_bank") or []
    return {row.get("concept") for row in rows}


def test_owner_keeps_three_companies_apart_and_manager_sees_one():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"owner-{suffix}@example.com", "password": "owner-password", "full_name": "Marta"},
        )
        assert owner.status_code == 201, owner.text
        owner_body = owner.json()
        headers = {"Authorization": f"Bearer {owner_body['token']}"}
        home_id = owner_body["user"]["tenant_id"]

        renamed = client.put(
            f"/api/v1/tenants/{home_id}",
            json={"name": "Restaurantes Norte"},
            headers=headers,
        )
        assert renamed.status_code == 200, renamed.text

        beach = client.post("/api/v1/companies", json={"name": "Chiringuitos Sur"}, headers=headers)
        flats = client.post("/api/v1/companies", json={"name": "Apartamentos Booking"}, headers=headers)
        assert beach.status_code == 201, beach.text
        assert flats.status_code == 201, flats.text
        beach_id = beach.json()["id"]
        flats_id = flats.json()["id"]

        sites = {
            home_id: [
                ("Restaurante Centro", "restaurant"),
                ("Restaurante Norte", "restaurant"),
                ("Restaurante Sur", "restaurant"),
                ("Bar de copas", "bar"),
            ],
            beach_id: [
                ("Chiringuito Levante", "chiringuito"),
                ("Chiringuito Poniente", "chiringuito"),
            ],
            flats_id: [
                ("Apartamentos Booking", "apartments"),
            ],
        }
        for company_id, rows in sites.items():
            for name, kind in rows:
                created = client.post(
                    f"/api/v1/companies/{company_id}/sites",
                    json={"name": name, "kind": kind},
                    headers=headers,
                )
                assert created.status_code == 201, created.text

        listing = client.get("/api/v1/companies", headers=headers)
        assert listing.status_code == 200, listing.text
        by_name = {item["name"]: item for item in listing.json()["items"]}
        assert set(by_name) == {"Restaurantes Norte", "Chiringuitos Sur", "Apartamentos Booking"}
        assert len(by_name["Restaurantes Norte"]["sites"]) == 4
        assert len(by_name["Chiringuitos Sur"]["sites"]) == 2
        assert by_name["Apartamentos Booking"]["sites"][0]["kind"] == "apartments"
        assert all(item["role"] == "owner" for item in by_name.values())

        manager = client.post(
            f"/api/v1/companies/{home_id}/members",
            json={"email": f"manager-{suffix}@example.com", "password": "manager-password", "name": "Lucia"},
            headers=headers,
        )
        assert manager.status_code == 201, manager.text

        signed_in = client.post(
            "/api/v1/auth/login",
            json={"email": f"manager-{suffix}@example.com", "password": "manager-password"},
        )
        assert signed_in.status_code == 200, signed_in.text
        manager_headers = {"Authorization": f"Bearer {signed_in.json()['token']}"}

        visible = client.get("/api/v1/companies", headers=manager_headers)
        assert visible.status_code == 200
        visible_items = visible.json()["items"]
        assert [item["id"] for item in visible_items] == [home_id]
        assert visible_items[0]["role"] == "manager"
        assert len(visible_items[0]["sites"]) == 4

        blocked_company = client.post("/api/v1/companies", json={"name": "Otra"}, headers=manager_headers)
        assert blocked_company.status_code == 403
        blocked_site = client.post(
            f"/api/v1/companies/{home_id}/sites",
            json={"name": "Extra", "kind": "bar"},
            headers=manager_headers,
        )
        assert blocked_site.status_code == 403
        blocked_book = client.get(f"/api/v1/companies/{beach_id}/sites", headers=manager_headers)
        assert blocked_book.status_code == 403

        restaurant_file = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": home_id},
            files={"file": ("norte.csv", io.BytesIO(_csv("COBRO RESTAURANTE", "+100,00")), "text/csv")},
        )
        beach_file = client.post(
            "/api/v1/bank-statements/upload",
            headers=headers,
            data={"tenant_id": beach_id},
            files={"file": ("sur.csv", io.BytesIO(_csv("COBRO CHIRINGUITO", "+50,00")), "text/csv")},
        )
        assert restaurant_file.status_code == 200, restaurant_file.text
        assert beach_file.status_code == 200, beach_file.text

        manager_restaurants = client.get(
            "/api/v1/reconciliation/status",
            params={"tenant_id": home_id},
            headers=manager_headers,
        )
        manager_beach = client.get(
            "/api/v1/reconciliation/status",
            params={"tenant_id": beach_id},
            headers=manager_headers,
        )
        owner_beach = client.get(
            "/api/v1/reconciliation/status",
            params={"tenant_id": beach_id},
            headers=headers,
        )
        assert manager_restaurants.status_code == 200
        assert "COBRO RESTAURANTE" in _concepts(manager_restaurants.json())
        assert "COBRO CHIRINGUITO" not in _concepts(manager_restaurants.json())
        assert manager_beach.status_code == 403
        assert owner_beach.status_code == 200
        assert _concepts(owner_beach.json()) == {"COBRO CHIRINGUITO"}
