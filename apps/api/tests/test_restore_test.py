"""infra/digitalocean/restore_test.py: the checks it runs on a restored database, and its
guards (WBS OPS-8). No DigitalOcean call is made: the API client is replaced."""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from cloudoptics.audit.log import Actor, record
from cloudoptics.platform.db import scope

INFRA = Path(__file__).resolve().parents[3] / "infra" / "digitalocean"
ADMIN_URL = os.environ.get(
    "CLOUDOPTICS_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
API_DIR = Path(__file__).resolve().parents[1]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("restore_test", INFRA / "restore_test.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["restore_test"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


rt = _load()

SOURCE = {
    "id": "src-id",
    "name": "cloudoptics-pg",
    "engine": "pg",
    "version": "16",
    "region": "lon1",
    "size": "db-s-1vcpu-1gb",
}


def test_restore_body_copies_the_source_and_names_the_backup() -> None:
    body = rt.restore_body(SOURCE, "cloudoptics-pg-restore-test-1", "proj", "2026-11-18T01:00:00Z")
    assert body["backup_restore"] == {
        "database_name": "cloudoptics-pg",
        "backup_created_at": "2026-11-18T01:00:00Z",
    }
    assert (body["size"], body["region"], body["num_nodes"]) == ("db-s-1vcpu-1gb", "lon1", 1)
    assert "restore-test" in body["tags"]


def test_only_restore_test_clusters_are_ever_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(rt.provision, "call", lambda m, p, b=None: calls.append((m, p)) or {})
    assert rt.delete_cluster("src-id", "cloudoptics-pg") is False
    assert calls == []
    assert rt.delete_cluster("tmp-id", rt.RESTORE_PREFIX + "202611190900") is True
    assert calls == [("DELETE", "/databases/tmp-id")]


def test_plan_makes_only_read_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, path))
        if path == "/databases":
            return {"databases": [SOURCE]}
        if path.endswith("/backups"):
            return {
                "backups": [
                    {"created_at": "2026-11-17T01:00:00Z"},
                    {"created_at": "2026-11-18T01:00:00Z"},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(rt.provision, "call", fake)
    args = argparse.Namespace(plan=True, backup=None, allow_ip=None, allow_empty=False, keep=False)
    result = rt.run(args)
    assert result.backup_created_at == "2026-11-18T01:00:00Z"
    assert {m for m, _ in calls} == {"GET"}


def test_a_failed_step_still_deletes_the_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, path))
        if (method, path) == ("GET", "/databases"):
            return {"databases": [SOURCE]}
        if path.endswith("/backups"):
            return {"backups": [{"created_at": "2026-11-18T01:00:00Z"}]}
        if (method, path) == ("POST", "/databases"):
            return {"database": {"id": "tmp-id", "name": body["name"] if body else ""}}
        if (method, path) == ("GET", "/databases/tmp-id"):
            raise SystemExit("GET /databases/tmp-id failed: 500")
        return {}

    monkeypatch.setattr(rt.provision, "call", fake)
    monkeypatch.setattr(rt.provision, "ensure_project", lambda: "proj")
    args = argparse.Namespace(
        plan=False, backup=None, allow_ip="203.0.113.7", allow_empty=False, keep=False
    )
    result = rt.run(args)
    assert not result.ok
    assert result.cluster_deleted
    assert ("DELETE", "/databases/tmp-id") in calls


def test_revision_of_reads_alembic_output() -> None:
    assert rt.revision_of("0034 (head)") == "0034"
    assert rt.revision_of("INFO  [alembic] Context impl\n0033\n") == "0033"
    assert rt.revision_of("") == ""


@pytest.fixture
def restored() -> Iterator[dict[str, str]]:
    """A migrated database whose tables belong to a non-superuser owner, like doadmin on a
    DigitalOcean cluster (FORCE row-level security applies to it)."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("CLOUDOPTICS_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"PostgreSQL not available: {exc}")
    name = f"cloudoptics_restore_{secrets.token_hex(4)}"
    owner = f"restore_owner_{secrets.token_hex(4)}"
    password = secrets.token_hex(12)
    admin.execute(f"CREATE DATABASE {name}")
    admin.execute(f"CREATE ROLE {owner} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'")
    base = ADMIN_URL.rsplit("/", 1)[0]
    super_url = f"{base}/{name}".replace("postgresql://", "postgresql+psycopg://")
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", super_url)
    command.upgrade(cfg, "head")
    host = super_url.split("@", 1)[1]
    try:
        yield {
            "super": super_url,
            "owner": f"postgresql+psycopg://{owner}:{password}@{host}",
            "role": owner,
        }
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"DROP ROLE IF EXISTS {owner}")
        admin.close()


SEED_TENANT = (
    "INSERT INTO tenant (id, name, country_code, display_currency) VALUES (:t, 'Acme', 'NG', 'NGN')"
)
SEED_USER = "INSERT INTO app_user (id, email, auth_subject) VALUES (:u, 'a@x.test', 's1')"
OWN_TABLES = """
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO %I', r.tablename,
                   current_setting('restore_test.owner'));
  END LOOP;
END $$
"""


async def _seed(urls: dict[str, str]) -> None:
    engine = create_async_engine(urls["super"])
    tenant = uuid.uuid4()
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(text(SEED_TENANT), {"t": tenant})
        await session.execute(text(SEED_USER), {"u": uuid.uuid4()})
        await scope(session, tenant_id=tenant)
        for action in ("workspace.created", "member.invited"):
            await record(session, tenant_id=tenant, actor=Actor("system"), action=action)
        await record(session, tenant_id=None, actor=Actor("staff"), action="fx.rate_set")
    async with AsyncSession(engine) as session, session.begin():
        # After the writes commit (their deferred triggers block ALTER TABLE): hand the
        # tables to the non-superuser owner, as on a DigitalOcean cluster.
        await session.execute(
            text("SELECT set_config('restore_test.owner', :o, true)"), {"o": urls["role"]}
        )
        await session.execute(text(OWN_TABLES))
    await engine.dispose()


async def test_checks_read_every_workspace_and_leave_rls_forced(
    restored: dict[str, str],
) -> None:
    await _seed(restored)
    result = rt.Result(started_at="t", source_cluster="s", restore_cluster="r")
    await rt.database_checks(restored["owner"], result, allow_empty=False)
    assert result.ok, result.checks
    assert result.tables["tenant"] == 1
    assert result.tables["audit_log"] == 3
    assert (result.audit_chains_verified, result.audit_chains_failed) == (2, 0)
    assert result.newest_audit_row_at

    with psycopg.connect(restored["super"].replace("+psycopg", "")) as conn:
        forced = conn.execute(
            "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'audit_log'"
        ).fetchone()
        assert forced == (True,)  # the check transaction was rolled back
        conn.execute("UPDATE audit_log SET action = 'edited' WHERE action = 'member.invited'")
        conn.commit()

    tampered = rt.Result(started_at="t", source_cluster="s", restore_cluster="r")
    await rt.database_checks(restored["owner"], tampered, allow_empty=False)
    assert (tampered.audit_chains_verified, tampered.audit_chains_failed) == (1, 1)
    assert not tampered.ok


async def test_an_empty_restore_fails_unless_allowed(restored: dict[str, str]) -> None:
    result = rt.Result(started_at="t", source_cluster="s", restore_cluster="r")
    await rt.database_checks(restored["super"], result, allow_empty=False)
    assert not result.ok
    allowed = rt.Result(started_at="t", source_cluster="s", restore_cluster="r")
    await rt.database_checks(restored["super"], allowed, allow_empty=True)
    assert allowed.ok
