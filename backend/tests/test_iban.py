"""A bank account is stored only when the IBAN length and checksum agree."""
from __future__ import annotations

import os
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.iban import iban_ok  # noqa: E402
from app.main import app  # noqa: E402

VALID = "ES9121000418450200051332"


def test_iban_checksum_rejects_a_missing_character():
    assert iban_ok(VALID)
    assert iban_ok("ES91 2100 0418 4502 0005 1332")
    assert not iban_ok(VALID[:-1])
    assert not iban_ok(VALID[:-1] + "0")
    assert not iban_ok("ES111")


def test_guided_setup_rejects_an_iban_that_does_not_check_out():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        owner = client.post(
            "/api/v1/auth/register",
            json={"email": f"iban-{suffix}@example.com", "password": "iban-password", "full_name": "Iban"},
        )
        assert owner.status_code == 201, owner.text
        headers = {"Authorization": f"Bearer {owner.json()['token']}"}
        saved = client.post(
            "/api/v1/companies/guided-setup",
            headers=headers,
            json={
                "companies": [{
                    "name": "Casa",
                    "places": [{"name": "Local", "kind": "public"}],
                    "accounts": [{
                        "bank_name": "Santander",
                        "iban": VALID[:-1],
                        "sources": ["cards"],
                        "place_names": ["Local"],
                    }],
                }]
            },
        )
        assert saved.status_code == 422, saved.text
        assert saved.json()["detail"] == "IBAN does not check out"
