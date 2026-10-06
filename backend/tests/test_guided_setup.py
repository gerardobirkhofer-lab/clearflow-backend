"""The short setup saves companies, places, and the accounts that cover them."""
from __future__ import annotations

import os
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_guided_setup_links_shared_and_separate_accounts():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"guide-{suffix}@example.com", "password": "guide-password", "full_name": "Guia"},
        )
        assert owner.status_code == 201, owner.text
        headers = {"Authorization": f"Bearer {owner.json()['token']}"}
        home_id = owner.json()["user"]["tenant_id"]

        saved = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "holding_name": "Grupo Guia",
                "companies": [
                    {
                        "name": "Restaurantes Norte",
                        "places": [
                            {"name": "Restaurante Centro", "kind": "restaurant"},
                            {"name": "Restaurante Norte", "kind": "restaurant"},
                            {"name": "Bar de copas", "kind": "bar"},
                        ],
                        "accounts": [
                            {
                                "bank_name": "Santander",
                                "iban": "ES91 2100 0418 4502 0005 1332",
                                "currency": "EUR",
                                "sources": ["cards"],
                                "place_names": ["Restaurante Centro", "Restaurante Norte"],
                            },
                            {
                                "bank_name": "BBVA",
                                "iban": "ES3600491500001234567890",
                                "currency": "EUR",
                                "sources": ["cards", "cash"],
                                "place_names": ["Bar de copas"],
                            },
                        ],
                    },
                    {
                        "name": "Apartamentos Booking",
                        "places": [{"name": "Pisos Centro", "kind": "apartments"}],
                        "accounts": [
                            {
                                "pending": True,
                                "place_names": ["Pisos Centro"],
                            }
                        ],
                    },
                ],
            },
        )
        assert saved.status_code == 201, saved.text
        body = saved.json()
        assert body["holding_name"] == "Grupo Guia"
        assert body["companies"][0]["id"] == home_id
        assert body["companies"][0]["name"] == "Restaurantes Norte"
        assert len(body["companies"][0]["sites"]) == 3
        shared = body["companies"][0]["accounts"][0]
        assert shared["place_names"] == ["Restaurante Centro", "Restaurante Norte"]
        assert shared["sources"] == ["cards"]
        assert shared["iban"] == "••••1332"
        assert "ES9121000418450200051332" not in saved.text
        assert body["companies"][1]["accounts"][0]["pending"] is True

        listed = client.get("/api/v1/companies", headers=headers)
        names = {item["name"] for item in listed.json()["items"]}
        assert names == {"Restaurantes Norte", "Apartamentos Booking"}

        mixed = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "companies": [
                    {
                        "name": "Solo",
                        "places": [{"name": "Un local", "kind": "bar"}],
                        "accounts": [{"bank_name": "ING", "iban": "ES9", "sources": ["cash"], "place_names": ["Otro"]}],
                    }
                ]
            },
        )
        assert mixed.status_code == 422

        manager = client.post(
            f"/api/v1/companies/{home_id}/members",
            headers=headers,
            json={"email": f"guide-mgr-{suffix}@example.com", "password": "manager-password", "name": "Nuria"},
        )
        assert manager.status_code == 201, manager.text
        signed = client.post(
            "/api/v1/auth/login",
            json={"email": f"guide-mgr-{suffix}@example.com", "password": "manager-password"},
        )
        blocked = client.post(
            "/api/v1/companies/guided-setup",
            headers={"Authorization": f"Bearer {signed.json()['token']}"},
            json={"companies": [{"name": "X", "places": [{"name": "Y", "kind": "bar"}], "accounts": []}]},
        )
        assert blocked.status_code == 403


def test_branches_can_share_a_name_when_the_location_differs():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"branch-{suffix}@example.com", "password": "branch-password", "full_name": "Sucursales"},
        )
        assert owner.status_code == 201, owner.text
        headers = {"Authorization": f"Bearer {owner.json()['token']}"}
        saved = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "companies": [{
                    "name": "Empanadas La Cala",
                    "places": [
                        {"name": "Empanadas La Cala", "location": "Marbella centro", "kind": "public"},
                        {"name": "Empanadas La Cala", "location": "Av. del Mar", "kind": "public"},
                    ],
                    "accounts": [{
                        "pending": True,
                        "place_names": [
                            "Empanadas La Cala · Marbella centro",
                            "Empanadas La Cala · Av. del Mar",
                        ],
                    }],
                }],
            },
        )
        assert saved.status_code == 201, saved.text
        sites = saved.json()["companies"][0]["sites"]
        assert {site["location"] for site in sites} == {"Marbella centro", "Av. del Mar"}
        assert {site["name"] for site in sites} == {"Empanadas La Cala"}

        repeated = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "companies": [{
                    "name": "Empanadas La Cala",
                    "places": [
                        {"name": "Empanadas La Cala", "kind": "public"},
                        {"name": "Empanadas La Cala", "kind": "public"},
                    ],
                    "accounts": [{"pending": True, "place_names": ["Empanadas La Cala"]}],
                }],
            },
        )
        assert repeated.status_code == 422, repeated.text
        assert repeated.json()["detail"] == "Places with the same name need a different location: Empanadas La Cala"
