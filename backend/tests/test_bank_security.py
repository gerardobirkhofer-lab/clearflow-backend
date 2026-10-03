"""Account numbers stay encrypted, bank deletion is real, and login limits survive in the database."""
from __future__ import annotations

import asyncio
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


def _query(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_bank_number_is_encrypted_and_deletion_removes_movements():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"secure-{suffix}@example.com", "password": "secure-password", "full_name": "Segura"},
        )
        assert owner.status_code == 201, owner.text
        token = owner.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        home_id = owner.json()["user"]["tenant_id"]

        saved = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "companies": [
                    {
                        "name": "Casa Segura",
                        "places": [{"name": "Local Uno", "kind": "bar"}],
                        "accounts": [{
                            "bank_name": "Santander",
                            "iban": IBAN,
                            "currency": "EUR",
                            "sources": ["cards"],
                            "place_names": ["Local Uno"],
                        }],
                    }
                ]
            },
        )
        assert saved.status_code == 201, saved.text
        account = saved.json()["companies"][0]["accounts"][0]
        assert account["iban"] == "••••7891"
        assert IBAN not in saved.text

        stored = _query("SELECT iban FROM bank_accounts WHERE tenant_id = $1", uuid.UUID(home_id))
        assert stored[0]["iban"].startswith("enc:v1:")
        assert decrypt_secret(stored[0]["iban"]) == IBAN

        listed = client.get(f"/api/v1/companies/{home_id}/bank-accounts", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["iban"] == "••••7891"

        _query(
            """
            INSERT INTO bank_transactions (tenant_id, bank_name, concept, amount, matched)
            VALUES ($1, 'Santander', 'Cobro', 10, 0)
            """,
            uuid.UUID(home_id),
        )
        removed = client.delete(
            f"/api/v1/companies/{home_id}/bank-accounts/{account['id']}",
            headers=headers,
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["movements_deleted"] == 1
        assert client.get(f"/api/v1/companies/{home_id}/bank-accounts", headers=headers).json()["items"] == []
        left = _query("SELECT count(*) AS n FROM bank_transactions WHERE tenant_id = $1", uuid.UUID(home_id))
        assert left[0]["n"] == 0
        events = _query(
            "SELECT action, detail FROM security_events WHERE tenant_id = $1 ORDER BY id",
            uuid.UUID(home_id),
        )
        actions = [row["action"] for row in events]
        assert actions == ["bank_account.saved", "bank_account.deleted"]
        assert IBAN not in "".join(row["detail"] or "" for row in events)

        hidden = client.get("/api/v1/institutions")
        assert hidden.status_code == 401
        banks = client.get("/api/v1/institutions", headers=headers)
        assert banks.status_code == 200, banks.text
        assert banks.json()["items"] == []
        assert "BBVA" not in banks.text

        manager = client.post(
            f"/api/v1/companies/{home_id}/members",
            headers=headers,
            json={"email": f"secure-mgr-{suffix}@example.com", "password": "manager-password", "name": "Nuria"},
        )
        assert manager.status_code == 201, manager.text
        signed = client.post(
            "/api/v1/auth/login",
            json={"email": f"secure-mgr-{suffix}@example.com", "password": "manager-password"},
        )
        blocked = client.delete(
            f"/api/v1/companies/{home_id}/bank-data",
            headers={"Authorization": f"Bearer {signed.json()['token']}"},
        )
        assert blocked.status_code == 403


def test_login_limit_is_stored():
    email = f"limit-{uuid.uuid4().hex[:8]}@example.com"
    with TestClient(app) as client:
        for _ in range(8):
            failed = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
            assert failed.status_code == 401, failed.text
        blocked = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
        assert blocked.status_code == 429, blocked.text
    rows = _query("SELECT count(*) AS n FROM auth_attempts WHERE attempt_key LIKE $1", f"%{email}%")
    assert rows[0]["n"] == 8
