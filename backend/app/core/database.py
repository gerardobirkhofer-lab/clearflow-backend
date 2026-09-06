"""
Database layer with tenant-aware routing.

- Shared (meta) DB: always holds the Tenant table and auth-related data.
- Tenant DB: Starter tenants live in the shared DB (schema-isolated by tenant_id).
  Pro/Enterprise tenants get a dedicated asyncpg engine.
"""
from __future__ import annotations

import uuid
from typing import AsyncGenerator

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy import select, MetaData

from .config import get_settings
from .auth import get_current_user, CurrentUser

settings = get_settings()


def _async_url(url: str) -> str:
    """Render gives postgres:// or postgresql://; asyncpg needs postgresql+asyncpg://.
    SQLite uses sqlite+aiosqlite://."""
    if url.startswith("sqlite:"):
        return url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


# ── Shared (meta) engine ──────────────────────────────────────────────────────
shared_engine = create_async_engine(
    _async_url(settings.DATABASE_URL),
    echo=settings.DEBUG,
    future=True,
)

SharedSessionLocal = async_sessionmaker(
    shared_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()

# Import legacy models so they register in Base.metadata
from app.models import bank_account, bank_transaction, dispute, dispute_email_log, expected_collection, provider, provider_connection, provider_transaction, user


# ── Tenant-aware DB Manager ───────────────────────────────────────────────────

class TenantDBManager:
    """
    Holds a per-tenant cache of async engines and session makers.

    Logic:
      * Starter (database_url is None)  → shared engine.
      * Pro / Enterprise (database_url set) → dedicated asyncpg engine.
    """

    def __init__(self, shared_engine: AsyncEngine):
        self.shared_engine = shared_engine
        self._engines: dict[uuid.UUID, AsyncEngine] = {}
        self._session_makers: dict[uuid.UUID, async_sessionmaker] = {}
        self._tenant_urls: dict[uuid.UUID, str | None] = {}

    async def _fetch_tenant_db_url(self, tenant_id: uuid.UUID) -> str | None:
        """Query the shared meta-DB for tenant.database_url (cached)."""
        if tenant_id in self._tenant_urls:
            return self._tenant_urls[tenant_id]

        async with SharedSessionLocal() as session:
            # Lazy import to avoid circular imports at module load time
            from app.models_orm import Tenant

            result = await session.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            tenant = result.scalar_one_or_none()
            if tenant is None:
                raise HTTPException(status_code=404, detail="Tenant not found")

            self._tenant_urls[tenant_id] = tenant.database_url
            return tenant.database_url

    async def get_session_maker(self, tenant_id: uuid.UUID) -> async_sessionmaker:
        """Return a cached session maker for the given tenant."""
        if tenant_id in self._session_makers:
            return self._session_makers[tenant_id]

        db_url = await self._fetch_tenant_db_url(tenant_id)

        if db_url is None:
            # Starter tier → shared schema isolation
            engine = self.shared_engine
        else:
            # Pro / Enterprise → dedicated DB
            engine = create_async_engine(
                _async_url(db_url),
                echo=settings.DEBUG,
                future=True,
                pool_pre_ping=True,
            )
            self._engines[tenant_id] = engine

        maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._session_makers[tenant_id] = maker
        return maker

    async def close_tenant(self, tenant_id: uuid.UUID) -> None:
        """Dispose a tenant's dedicated engine (useful on downgrade or delete)."""
        engine = self._engines.pop(tenant_id, None)
        if engine is not None:
            await engine.dispose()
        self._session_makers.pop(tenant_id, None)
        self._tenant_urls.pop(tenant_id, None)

    async def close_all(self) -> None:
        """Dispose all tenant-dedicated engines."""
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()
        self._session_makers.clear()
        self._tenant_urls.clear()


# Global singleton
tenant_db_manager = TenantDBManager(shared_engine)


# ── DB Dependency for FastAPI ─────────────────────────────────────────────────

async def get_db(
    current_user: CurrentUser = Depends(get_current_user),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession from the shared DB (demo mode)."""
    async with SharedSessionLocal() as session:
        yield session


# ── Legacy helpers ────────────────────────────────────────────────────────────

async def get_shared_db() -> AsyncGenerator[AsyncSession, None]:
    """Explicit dependency for endpoints that MUST touch the shared meta-DB."""
    async with SharedSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables in the shared (meta) database on startup.
    Combines legacy and new ORM tables, skipping duplicates.
    Uses checkfirst=True to avoid errors if tables already exist."""
    from app import models_orm as orm

    combined = MetaData()

    # Add legacy tables first (skip 'users' which conflicts with new ORM)
    for name, table in Base.metadata.tables.items():
        if name != "users":
            table.tometadata(combined)

    # Add new ORM tables
    for name, table in orm.Base.metadata.tables.items():
        table.tometadata(combined)

    try:
        async with shared_engine.begin() as conn:
            # checkfirst=True is default, but we make it explicit for safety
            await conn.run_sync(lambda sync_conn: combined.create_all(sync_conn, checkfirst=True))
    except Exception as e:
        # Log but don't crash — tables likely already exist
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"DB init warning (tables likely exist): {e}")

    # Ensure default tenant exists (required for demo user)
    async with SharedSessionLocal() as session:
        from app.models_orm import Tenant
        result = await session.execute(
            select(Tenant).where(Tenant.id == uuid.UUID("22222222-2222-2222-2222-222222222222"))
        )
        if result.scalar_one_or_none() is None:
            default_tenant = Tenant(
                id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                name="Default Tenant",
                slug="default",
                timezone="UTC",
                currency="USD",
                is_active=True,
                subscription_plan="free",
                tier="starter",
            )
            session.add(default_tenant)
            await session.commit()
    """Create all tables in the shared (meta) database on startup.
    Combines legacy and new ORM tables, skipping duplicates."""
    from app import models_orm as orm

    combined = MetaData()

    # Add legacy tables first (skip 'users' which conflicts with new ORM)
    for name, table in Base.metadata.tables.items():
        if name != "users":
            table.tometadata(combined)

    # Add new ORM tables
    for name, table in orm.Base.metadata.tables.items():
        table.tometadata(combined)

    async with shared_engine.begin() as conn:
        await conn.run_sync(combined.create_all)

    # Ensure default tenant exists (required for demo user)
    async with SharedSessionLocal() as session:
        from app.models_orm import Tenant
        result = await session.execute(
            select(Tenant).where(Tenant.id == uuid.UUID("22222222-2222-2222-2222-222222222222"))
        )
        if result.scalar_one_or_none() is None:
            default_tenant = Tenant(
                id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                name="Default Tenant",
                slug="default",
                timezone="UTC",
                currency="USD",
                is_active=True,
                subscription_plan="free",
                tier="starter",
            )
            session.add(default_tenant)
            await session.commit()
