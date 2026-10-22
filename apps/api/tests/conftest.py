"""Test fixtures backed by a real PostgreSQL (RLS cannot be tested against SQLite).

Uses ``CLOUDOPTICS_TEST_ADMIN_URL`` (a superuser URL, default: local postgres/postgres).
Each test session creates a throwaway database, migrates it and gives the app role a
login. Database tests are skipped when PostgreSQL is unreachable, unless
``CLOUDOPTICS_REQUIRE_DB=1`` (set in CI) makes that a failure.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

ADMIN_URL = os.environ.get(
    "CLOUDOPTICS_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
APP_PASSWORD = "app-test-password"  # noqa: S105 - throwaway test database
API_DIR = Path(__file__).resolve().parents[1]


def _with_db(url: str, db: str) -> str:
    return url.rsplit("/", 1)[0] + f"/{db}"


@pytest.fixture(scope="session")
def database() -> Iterator[dict[str, str]]:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("CLOUDOPTICS_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"PostgreSQL not available: {exc}")
    name = f"cloudoptics_test_{secrets.token_hex(4)}"
    admin.execute(f"CREATE DATABASE {name}")
    owner_url = _with_db(ADMIN_URL, name).replace("postgresql://", "postgresql+psycopg://")

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", owner_url)
    command.upgrade(cfg, "head")
    admin.execute(f"ALTER ROLE cloudoptics_app LOGIN PASSWORD '{APP_PASSWORD}'")

    user_part = owner_url.split("://", 1)[1].split("@", 1)[1]
    app_url = f"postgresql+psycopg://cloudoptics_app:{APP_PASSWORD}@{user_part}"
    try:
        yield {"owner": owner_url, "app": app_url, "name": name}
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


@pytest.fixture(scope="session")
def app(database: dict[str, str]):  # type: ignore[no-untyped-def]
    os.environ["CLOUDOPTICS_ENVIRONMENT"] = "test"
    os.environ["CLOUDOPTICS_DATABASE_URL"] = database["owner"]
    os.environ["CLOUDOPTICS_APP_DATABASE_URL"] = database["app"]
    os.environ["CLOUDOPTICS_AUTH_MODE"] = "dev"
    from cloudoptics.identity.auth import get_verifier
    from cloudoptics.platform.db import get_engine, get_sessionmaker
    from cloudoptics.platform.settings import get_settings

    for cached in (get_settings, get_engine, get_sessionmaker, get_verifier):
        cached.cache_clear()
    from cloudoptics.main import create_app

    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def owner_sql(database: dict[str, str]) -> Iterator[Callable[..., list[tuple[object, ...]]]]:
    """Run SQL as the schema owner (bypasses nothing: tables use FORCE RLS, so the owner
    connects as superuser here to set up and tamper with data)."""
    conn = psycopg.connect(_with_db(ADMIN_URL, database["name"]), autocommit=True)

    def run(sql: str, *params: object) -> list[tuple[object, ...]]:
        cur = conn.execute(sql, params or None)
        return cur.fetchall() if cur.description else []

    yield run
    conn.close()


@pytest.fixture
def login(client: AsyncClient) -> Callable[..., object]:
    async def _login(email: str | None = None) -> dict[str, str]:
        email = email or f"user-{secrets.token_hex(3)}@example.com"
        r = await client.post("/v1/dev/token", json={"email": email, "name": "Test User"})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['accessToken']}"}

    return _login
