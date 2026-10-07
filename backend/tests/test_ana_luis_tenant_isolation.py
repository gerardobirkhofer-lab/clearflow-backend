"""Ana and Luis do not share money, and the dashboard requires a real token."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import jwt
import psycopg2
import pytest
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["JWT_SECRET_KEY"] = "test-tenant-isolation-secret"

from app.main import app  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def _sync_dsn() -> str:
    parsed = urlparse(get_settings().DATABASE_URL.replace("+asyncpg", ""))
    return (
        f"host={parsed.hostname} port={parsed.port or 5432} "
        f"dbname={parsed.path.lstrip('/')} user={parsed.username} password={parsed.password}"
    )


def _insert_luis_money(tenant_id: str) -> None:
    now = datetime.now(timezone.utc)
    with psycopg2.connect(_sync_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bank_transactions
                    (tenant_id, amount, balance, matched, transaction_date, concept)
                VALUES (%s, %s, %s, 0, %s, 'Nomina de Luis')
                """,
                (tenant_id, 4200, 4200, now),
            )
            cur.execute(
                """
                INSERT INTO provider_transactions
                    (tenant_id, provider_name, amount, matched, transaction_date, concept)
                VALUES (%s, 'stripe', %s, 0, %s, 'Ventas de Luis')
                """,
                (tenant_id, 1800, now),
            )
        conn.commit()


def _money(payload: dict, field: str) -> Decimal:
    return Decimal(str(payload[field]))


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_marks_tenant_auth(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["deploy"] == "auth-tenant-v1"


def test_ana_does_not_see_luis_money(client: TestClient):
    suffix = uuid.uuid4().hex[:8]
    ana_email = f"ana-{suffix}@example.com"
    luis_email = f"luis-{suffix}@example.com"

    anonymous = client.get("/api/v1/dashboard/summary")
    assert anonymous.status_code == 401

    forged = client.get(
        "/api/v1/dashboard/summary",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert forged.status_code == 401

    ana_register = client.post(
        "/api/v1/auth/register",
        json={"email": ana_email, "password": "ana-password", "full_name": "Ana"},
    )
    luis_register = client.post(
        "/api/v1/auth/register",
        json={"email": luis_email, "password": "luis-password", "full_name": "Luis"},
    )
    assert ana_register.status_code == 201
    assert luis_register.status_code == 201

    ana_login = client.post(
        "/api/v1/auth/login",
        json={"email": ana_email, "password": "ana-password"},
    )
    luis_login = client.post(
        "/api/v1/auth/login",
        json={"email": luis_email, "password": "luis-password"},
    )
    assert ana_login.status_code == 200
    assert luis_login.status_code == 200

    ana = ana_login.json()
    luis = luis_login.json()
    assert ana["user"]["name"] == "Ana"
    assert luis["user"]["name"] == "Luis"
    assert ana["user"]["tenant_id"]
    assert ana["user"]["tenant_id"] == ana_register.json()["user"]["tenant_id"]
    assert luis["user"]["tenant_id"] == luis_register.json()["user"]["tenant_id"]
    assert ana["user"]["tenant_id"] != luis["user"]["tenant_id"]

    _insert_luis_money(luis["user"]["tenant_id"])

    ana_headers = {"Authorization": f"Bearer {ana['token']}"}
    luis_headers = {"Authorization": f"Bearer {luis['token']}"}

    ana_dashboard = client.get("/api/v1/dashboard/summary", headers=ana_headers)
    luis_dashboard = client.get("/api/v1/dashboard/summary", headers=luis_headers)
    assert ana_dashboard.status_code == 200
    assert luis_dashboard.status_code == 200

    ana_body = ana_dashboard.json()
    luis_body = luis_dashboard.json()
    assert _money(ana_body, "bank_balance") == 0
    assert _money(ana_body, "today_collections") == 0
    assert _money(ana_body, "yesterday_collections") == 0
    assert _money(luis_body, "bank_balance") == Decimal("4200")
    assert _money(luis_body, "today_collections") == Decimal("1800")
    assert _money(luis_body, "yesterday_collections") == Decimal("4200")

    spoofed = client.get(
        "/api/v1/dashboard/summary",
        params={"tenant_id": luis["user"]["tenant_id"]},
        headers=ana_headers,
    )
    assert spoofed.status_code == 200
    spoofed_body = spoofed.json()
    assert _money(spoofed_body, "bank_balance") == 0
    assert _money(spoofed_body, "today_collections") == 0
    assert _money(spoofed_body, "yesterday_collections") == 0

    claims = jwt.decode(ana["token"], options={"verify_signature": False})
    claims["tenant_id"] = luis["user"]["tenant_id"]
    swapped = jwt.encode(claims, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    swapped_dashboard = client.get(
        "/api/v1/dashboard/summary",
        headers={"Authorization": f"Bearer {swapped}"},
    )
    assert swapped_dashboard.status_code == 200
    swapped_body = swapped_dashboard.json()
    assert _money(swapped_body, "bank_balance") == 0
    assert _money(swapped_body, "today_collections") == 0
    assert _money(swapped_body, "yesterday_collections") == 0


def test_login_creates_tenant_for_account_without_one(client: TestClient):
    from app.api.v1.auth import _hash_password

    suffix = uuid.uuid4().hex[:8]
    email = f"legacy-{suffix}@example.com"
    with psycopg2.connect(_sync_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cf_local_users (email, password_hash, name, role, is_active)
                VALUES (%s, %s, 'Legacy', 'self_owner', 1)
                """,
                (email, _hash_password("legacy-password")),
            )
        conn.commit()

    first = client.post("/api/v1/auth/login", json={"email": email, "password": "legacy-password"})
    second = client.post("/api/v1/auth/login", json={"email": email, "password": "legacy-password"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user"]["tenant_id"]
    assert second.json()["user"]["tenant_id"] == first.json()["user"]["tenant_id"]


def test_accounts_cannot_see_or_write_each_other(client: TestClient):
    suffix = uuid.uuid4().hex[:8]
    ana = client.post(
        "/api/v1/auth/register",
        json={"email": f"ana-iso-{suffix}@example.com", "password": "ana-password", "full_name": "Ana"},
    )
    luis = client.post(
        "/api/v1/auth/register",
        json={"email": f"luis-iso-{suffix}@example.com", "password": "luis-password", "full_name": "Luis"},
    )
    assert ana.status_code == 201
    assert luis.status_code == 201
    ana_body = ana.json()
    luis_body = luis.json()
    ana_headers = {"Authorization": f"Bearer {ana_body['token']}"}
    luis_tenant = luis_body["user"]["tenant_id"]

    listing = client.get("/api/v1/tenants/", headers=ana_headers)
    assert listing.status_code == 200
    ids = [item["id"] for item in listing.json()["items"]]
    assert ids == [ana_body["user"]["tenant_id"]]
    assert "database_url" not in listing.json()["items"][0]

    blocked = client.put(
        f"/api/v1/tenants/{luis_tenant}",
        json={"name": "Taken"},
        headers=ana_headers,
    )
    assert blocked.status_code == 403

    upload = client.post(
        "/api/v1/bank-statements/upload",
        headers=ana_headers,
        data={"tenant_id": luis_tenant},
        files={"file": ("mov.csv", "fecha,concepto,importe\n01/01/2026,Nomina,10\n", "text/csv")},
    )
    assert upload.status_code == 403

    stripe_status = client.get(f"/api/v1/stripe/status/{luis_tenant}", headers=ana_headers)
    assert stripe_status.status_code == 403

    unsigned = client.post("/api/v1/stripe/webhook", json={"type": "checkout.session.completed"})
    assert unsigned.status_code == 400

    short = client.post(
        "/api/v1/auth/register",
        json={"email": f"short-{suffix}@example.com", "password": "short", "full_name": "Short"},
    )
    assert short.status_code == 422

    saved = client.put(
        "/api/v1/account/profile",
        json={"payload": {"societies": [{"name": "Cafe Norte"}]}, "onboarding_complete": True},
        headers=ana_headers,
    )
    assert saved.status_code == 200
    loaded = client.get("/api/v1/account/profile", headers=ana_headers)
    assert loaded.status_code == 200
    assert loaded.json()["payload"]["societies"][0]["name"] == "Cafe Norte"
    assert loaded.json()["onboarding_complete"] is True

    from app.api.v1.auth import _reset_token

    token = _reset_token(ana_body["user"]["id"], ana_body["user"]["email"])
    reset = client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "password": "ana-password-2"},
    )
    assert reset.status_code == 200
    relogin = client.post(
        "/api/v1/auth/login",
        json={"email": ana_body["user"]["email"], "password": "ana-password-2"},
    )
    assert relogin.status_code == 200
