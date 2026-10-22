"""Database access with tenant isolation by PostgreSQL row-level security.

Every request runs in one transaction. ``scope`` sets ``app.user_id`` and ``app.tenant_id``
with ``set_config(..., is_local => true)``, so they vanish at commit and cannot leak to the
next request that reuses the pooled connection. Policies read them through
``app_current_user()`` / ``app_current_tenant()`` (migration 0001).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cloudoptics.platform.settings import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().app_database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request, rolled back on error."""
    async with get_sessionmaker()() as session, session.begin():
        yield session


async def scope(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        text("SELECT set_config('app.user_id', :u, true), set_config('app.tenant_id', :t, true)"),
        {"u": str(user_id) if user_id else "", "t": str(tenant_id) if tenant_id else ""},
    )
