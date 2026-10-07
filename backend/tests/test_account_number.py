"""Spain keeps the IBAN check. Argentina uses CBU or CVU. Other countries are stored unchecked."""
from __future__ import annotations

import os
import uuid

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clearflow:clearflow_dev_password_2024@127.0.0.1:5432/clearflow_tenant_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-tenant-isolation-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.account_number import cbu_ok  # noqa: E402
from app.main import app  # noqa: E402

CBU = "2850590940090418135201"
CVU = "0000003110000000000014"
GERMAN = "DE89370400440532013000"
SPANISH = "ES9121000418450200051332"


def test_cbu_checksum_rejects_a_missing_or_wrong_digit():
    assert cbu_ok(CBU)
    assert cbu_ok("2850 5909 4009 0418 1352 01")
    assert cbu_ok(CVU)
    assert cbu_ok("0170099220000067797370")
    assert not cbu_ok(CBU[:-1])
    assert not cbu_ok(CBU[:-1] + "0")
    assert not cbu_ok("ES9121000418450200051332")


def _save(client, headers, number: str, country: str | None, currency: str = "EUR"):
    account = {
        "bank_name": "Banco",
        "iban": number,
        "currency": currency,
        "sources": ["cards"],
        "place_names": ["Local"],
    }
    if country is not None:
        account["country"] = country
    return client.post(
        "/api/v1/companies/guided-setup",
        headers=headers,
        json={"companies": [{"name": "Casa", "places": [{"name": "Local", "kind": "public"}], "accounts": [account]}]},
    )


def _owner(client):
    suffix = uuid.uuid4().hex[:8]
    owner = client.post(
        "/api/v1/auth/register",
        json={"email": f"acct-{suffix}@example.com", "password": "acct-password", "full_name": "Cuenta"},
    )
    assert owner.status_code == 201, owner.text
    return {"Authorization": f"Bearer {owner.json()['token']}"}


def test_argentine_cbu_is_stored_and_a_short_one_is_rejected():
    with TestClient(app) as client:
        headers = _owner(client)
        saved = _save(client, headers, "2850 5909 4009 0418 1352 01", "AR", "EUR")
        assert saved.status_code == 201, saved.text
        account = saved.json()["companies"][0]["accounts"][0]
        assert account["iban"] == "••••5201"
        assert account["country"] == "AR"
        assert account["checked"] is True
        assert account["currency"] == "ARS"
        assert CBU not in saved.text

        rejected = _save(client, headers, CBU[:-1], "AR")
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"] == "CBU does not check out"


def test_another_country_stores_an_unchecked_number_and_still_checks_an_iban():
    with TestClient(app) as client:
        headers = _owner(client)
        raw = _save(client, headers, "99887766", "OTHER")
        assert raw.status_code == 201, raw.text
        account = raw.json()["companies"][0]["accounts"][0]
        assert account["iban"] == "••••7766"
        assert account["country"] == "XX"
        assert account["checked"] is False
        assert "99887766" not in raw.text

        german = _save(client, headers, GERMAN, "OTHER")
        assert german.status_code == 201, german.text
        checked = german.json()["companies"][0]["accounts"][0]
        assert checked["country"] == "DE"
        assert checked["checked"] is True
        assert checked["iban"] == "••••3000"
        assert GERMAN not in german.text

        short_spanish = _save(client, headers, SPANISH[:-1], "OTHER")
        assert short_spanish.status_code == 422, short_spanish.text
        assert short_spanish.json()["detail"] == "IBAN does not check out"
