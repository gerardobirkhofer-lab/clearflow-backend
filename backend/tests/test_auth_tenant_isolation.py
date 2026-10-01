"""Each LocalAuthUser gets a private tenant. Missing tokens are rejected."""
from __future__ import annotations

import os
from pathlib import Path

DB_PATH = Path("/tmp/clearflow_auth_tenant.db")
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"

import asyncio

import bcrypt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import auth as auth_routes
from app.api.v1 import tenants as tenant_routes
from app.core.auth import SHARED_TENANT_ID
from app.core.database import SharedSessionLocal, init_db
from app.models.local_auth_user import LocalAuthUser

DEMO = str(SHARED_TENANT_ID)


def _client() -> TestClient:
    asyncio.run(init_db())
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1/auth")
    app.include_router(tenant_routes.router, prefix="/api/v1/tenants")
    return TestClient(app)


def test_users_receive_distinct_tenants_and_demo_is_not_a_fallback():
    with _client() as client:
        first = client.post(
            "/api/v1/auth/register",
            json={"email": "ana@example.com", "password": "secret12", "full_name": "Ana"},
        )
        second = client.post(
            "/api/v1/auth/register",
            json={"email": "luis@example.com", "password": "secret12", "full_name": "Luis"},
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text

        ana_tenant = first.json()["user"]["tenant_id"]
        luis_tenant = second.json()["user"]["tenant_id"]
        assert ana_tenant != luis_tenant
        assert ana_tenant != DEMO
        assert luis_tenant != DEMO

        ana_me = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {first.json()['token']}"},
        )
        luis_me = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {second.json()['token']}"},
        )
        assert ana_me.status_code == 200, ana_me.text
        assert luis_me.status_code == 200, luis_me.text
        assert ana_me.json()["id"] == ana_tenant
        assert luis_me.json()["id"] == luis_tenant

        missing = client.get("/api/v1/tenants/me")
        forged = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert missing.status_code == 401
        assert forged.status_code == 401
        assert DEMO not in missing.text
        assert DEMO not in forged.text

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "ana@example.com", "password": "secret12"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["tenant_id"] == ana_tenant


def test_legacy_user_without_tenant_is_not_mapped_to_demo():
    with _client() as client:
        async def insert_legacy():
            password_hash = bcrypt.hashpw(b"secret12", bcrypt.gensalt(rounds=4)).decode()
            async with SharedSessionLocal() as session:
                user = LocalAuthUser(
                    email="legacy@example.com",
                    password_hash=password_hash,
                    name="Legacy",
                    role="self_owner",
                    is_active=1,
                    tenant_id=None,
                )
                session.add(user)
                await session.commit()

        asyncio.run(insert_legacy())

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "legacy@example.com", "password": "secret12"},
        )
        assert login.status_code == 200, login.text
        tenant_id = login.json()["user"]["tenant_id"]
        assert tenant_id and tenant_id != DEMO

        me = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
        )
        assert me.status_code == 200, me.text
        assert me.json()["id"] == tenant_id
        assert me.json()["id"] != DEMO


def test_shared_tenant_assignment_and_unknown_subject_are_rejected():
    import jwt
    from datetime import datetime, timedelta, timezone

    from app.core.auth import ALGORITHM, SECRET_KEY

    with _client() as client:
        async def insert_shared():
            password_hash = bcrypt.hashpw(b"secret12", bcrypt.gensalt(rounds=4)).decode()
            async with SharedSessionLocal() as session:
                user = LocalAuthUser(
                    email="shared@example.com",
                    password_hash=password_hash,
                    name="Shared",
                    role="self_owner",
                    is_active=1,
                    tenant_id=SHARED_TENANT_ID,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                return user.id

        user_id = asyncio.run(insert_shared())
        token = jwt.encode(
            {
                "sub": str(user_id),
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        me = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["id"] != DEMO

        unknown = jwt.encode(
            {"sub": "999999", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        rejected = client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {unknown}"},
        )
        assert rejected.status_code == 401
        assert DEMO not in rejected.text
