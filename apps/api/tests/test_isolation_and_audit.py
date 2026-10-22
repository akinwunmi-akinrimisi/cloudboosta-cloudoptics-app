"""Row-level security and audit-chain tests, run as the real app role."""

import uuid

import psycopg
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.platform.db import get_sessionmaker, scope


async def _tenant(client: AsyncClient, login) -> tuple[str, dict[str, str]]:
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": "T", "countryCode": "GH"}, headers=headers)
    return r.json()["id"], headers


async def test_rls_hides_other_tenants_rows(client: AsyncClient, login, app) -> None:
    a, _ = await _tenant(client, login)
    b, _ = await _tenant(client, login)
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=uuid.UUID(a))
        ids = {str(r[0]) for r in await s.execute(text("SELECT id FROM tenant"))}
        assert ids == {a}
    async with get_sessionmaker()() as s, s.begin():
        assert (await s.execute(text("SELECT count(*) FROM tenant"))).scalar_one() == 0
        assert (await s.execute(text("SELECT count(*) FROM membership"))).scalar_one() == 0
    assert b != a


async def test_rls_blocks_writes_into_another_tenant(client: AsyncClient, login, app) -> None:
    a, _ = await _tenant(client, login)
    b, _ = await _tenant(client, login)
    async with get_sessionmaker()() as s:
        with pytest.raises(Exception, match="row-level security"):
            async with s.begin():
                await scope(s, tenant_id=uuid.UUID(a))
                await s.execute(
                    text(
                        "INSERT INTO cloud_connection (id, tenant_id, aws_account_id, "
                        "account_type, role_arn, external_id_enc, stack_name) VALUES "
                        "(:id, :t, '123456789012', 'standalone', 'arn', '\\x00', 's')"
                    ),
                    {"id": uuid.uuid4(), "t": b},
                )


async def test_context_does_not_leak_between_transactions(client: AsyncClient, login, app) -> None:
    a, _ = await _tenant(client, login)
    async with get_sessionmaker()() as s:
        async with s.begin():
            await scope(s, tenant_id=uuid.UUID(a))
        async with s.begin():
            assert (await s.execute(text("SELECT count(*) FROM tenant"))).scalar_one() == 0


async def test_audit_chain_verifies_and_detects_tampering(
    client: AsyncClient, login, app, owner_sql
) -> None:
    tenant_id, headers = await _tenant(client, login)
    for name in ("one", "two", "three"):
        await client.patch(f"/v1/tenants/{tenant_id}", json={"name": name}, headers=headers)
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=uuid.UUID(tenant_id))
        assert await audit.verify_chain(s, uuid.UUID(tenant_id))

    owner_sql(
        'UPDATE audit_log SET after = \'{"name": "forged"}\' '
        "WHERE id = (SELECT max(id) FROM audit_log WHERE tenant_id = %s)",
        tenant_id,
    )
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=uuid.UUID(tenant_id))
        assert not await audit.verify_chain(s, uuid.UUID(tenant_id))


async def test_app_role_cannot_edit_or_delete_audit_rows(database: dict[str, str]) -> None:
    url = database["app"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        for sql in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
            conn.rollback()


async def test_app_role_has_no_rls_bypass(database: dict[str, str]) -> None:
    url = database["app"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)
