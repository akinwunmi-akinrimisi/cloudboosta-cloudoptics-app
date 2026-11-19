"""Every tenant table, found in the catalogue (not a hand list), is isolated for the real app
role under FORCE RLS: a row of workspace A is invisible to workspace B and cannot be updated,
deleted, written or moved into from B (multi-cloud plan, Phase 0).

Rows come from the data-deletion seeders, so a new tenant table must be seeded there (that
guard test asks for it too); this test fails on a tenant table with no seeded row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from psycopg import sql
from test_data_deletion import account_id, connection, seed_connection, seed_workspace, workspace

Sql = Callable[..., list[tuple[Any, ...]]]

TENANT_TABLES_SQL = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
    "AND NOT a.attisdropped WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
    "UNION SELECT 'tenant' ORDER BY 1"
)
INSERTABLE_COLUMNS_SQL = (
    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
    "AND NOT attisdropped AND attgenerated = '' AND attidentity <> 'a' ORDER BY attnum"
)
# Written only by triggers as the owner; the app role has no INSERT/UPDATE/DELETE on them,
# so they get the read checks (test_rls_lint.TENANT_EXCEPTIONS has the reasons).
TRIGGER_ONLY = {"tenant_data_version", "data_version_pending"}


def _column(table: str) -> str:
    return "id" if table == "tenant" else "tenant_id"


@pytest.fixture
def as_app(database: dict[str, str]) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database["app"].replace("postgresql+psycopg://", "postgresql://"))
    yield conn
    conn.close()


def _in_scope(conn: psycopg.Connection, tenant: str, user: str) -> None:
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)",
        (tenant, user),
    )


def _refused(conn: psycopg.Connection, tenant: str, user: str, query: sql.Composable, *args: Any):
    """Run ``query`` in B's scope and roll back. Returns the row count, or the error text
    when PostgreSQL refused it."""
    try:
        with conn.transaction(force_rollback=True):
            _in_scope(conn, tenant, user)
            return conn.execute(query, args or None).rowcount
    except psycopg.Error as exc:
        return str(exc)


def _blocked(outcome: int | str) -> bool:
    """Refused by RLS or by privileges (both keep the row out)."""
    return isinstance(outcome, str) and (
        "row-level security" in outcome or "permission denied" in outcome
    )


async def _user_id(client: AsyncClient, headers: dict[str, str]) -> str:
    return str((await client.get("/v1/me", headers=headers)).json()["id"])


async def test_every_tenant_table_is_isolated_for_the_app_role(
    client: AsyncClient, login, owner_sql: Sql, as_app: psycopg.Connection
) -> None:
    a, a_admin = await workspace(client, login, "Tenant A")
    b, b_admin = await workspace(client, login, "Tenant B")
    for tenant, headers in ((a, a_admin), (b, b_admin)):
        account = account_id()
        conn = await connection(client, tenant, headers, account)
        seed_connection(owner_sql, tenant, conn, account)
        seed_workspace(owner_sql, tenant)
        owner_sql(
            "INSERT INTO data_deletion (id, tenant_id, kind, target_label, status, scheduled_for) "
            "VALUES (%s, %s, 'tenant', 'x', 'cancelled', now())",
            uuid.uuid4(),
            tenant,
        )
    b_user = await _user_id(client, b_admin)

    tables = [r[0] for r in owner_sql(TENANT_TABLES_SQL)]
    assert len(tables) > 30
    unseeded = [
        t
        for t in tables
        if t not in TRIGGER_ONLY
        and owner_sql(
            sql.SQL("SELECT count(*) FROM {} WHERE {} = %s")
            .format(sql.Identifier(t), sql.Identifier(_column(t)))
            .as_string(),
            a,
        )
        == [(0,)]
    ]
    assert unseeded == [], "seed these tables in tests/test_data_deletion.py"

    leaks: list[str] = []
    for table in tables:
        t, col = sql.Identifier(table), sql.Identifier(_column(table))
        # Reads: nothing outside B is visible, A's rows included.
        seen = _refused(
            as_app,
            b,
            b_user,
            sql.SQL("SELECT 1 FROM {} WHERE {} IS DISTINCT FROM %s::uuid").format(t, col),
            b,
        )
        if not (seen == 0 or _blocked(seen)):
            leaks.append(f"{table}: B sees {seen} rows outside B")
        if table in TRIGGER_ONLY:
            continue
        # Updates and deletes of A's rows touch nothing.
        for verb, query in (
            ("UPDATE", sql.SQL("UPDATE {} SET {} = {} WHERE {} = %s").format(t, col, col, col)),
            ("DELETE", sql.SQL("DELETE FROM {} WHERE {} = %s").format(t, col)),
        ):
            outcome = _refused(as_app, b, b_user, query, a)
            if not (outcome == 0 or _blocked(outcome)):
                leaks.append(f"{table}: B's {verb} reached A ({outcome})")
        # Inserting a row for A (a copy of A's own row) is refused.
        columns = [r[0] for r in owner_sql(INSERTABLE_COLUMNS_SQL, table)]
        row = owner_sql(
            sql.SQL("SELECT row_to_json(x)::text FROM {} x WHERE {} = %s LIMIT 1")
            .format(t, col)
            .as_string(),
            a,
        )[0][0]
        names = sql.SQL(", ").join(map(sql.Identifier, columns))
        insert = sql.SQL("INSERT INTO {} ({}) SELECT {} FROM json_populate_record(NULL::{}, %s)")
        outcome = _refused(as_app, b, b_user, insert.format(t, names, names, t), row)
        if not _blocked(outcome):
            leaks.append(f"{table}: B inserted a row for A ({outcome})")
        # Moving one of B's own rows into A is refused.
        outcome = _refused(
            as_app,
            b,
            b_user,
            sql.SQL("UPDATE {} SET {} = %s WHERE {} = %s").format(t, col, col),
            a,
            b,
        )
        if not _blocked(outcome):
            leaks.append(f"{table}: B moved a row into A ({outcome})")
    assert leaks == [], "\n".join(leaks)
    # Sanity: B's own rows are visible to B, so the checks above ran on a working scope.
    assert _refused(as_app, b, b_user, sql.SQL("SELECT 1 FROM budget")) == 1


# --- app_user (migration 0041) ----------------------------------------------------------

APP_USER_EMAILS = sql.SQL("SELECT email::text FROM app_user ORDER BY 1")


def _emails(conn: psycopg.Connection, tenant: str | None, user: str | None) -> set[str]:
    with conn.transaction(force_rollback=True):
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)",
            (tenant or "", user or ""),
        )
        return {r[0] for r in conn.execute(APP_USER_EMAILS)}


async def test_app_user_rows_are_visible_only_to_self_and_co_members(
    client: AsyncClient, login, owner_sql: Sql, as_app: psycopg.Connection
) -> None:
    a_email, b_email, c_email = (f"{n}-{uuid.uuid4().hex[:6]}@example.com" for n in "abc")
    a_headers = await login(a_email)
    r = await client.post("/v1/tenants", json={"name": "A", "countryCode": "NG"}, headers=a_headers)
    a = r.json()["id"]
    b_headers = await login(b_email)
    r = await client.post("/v1/tenants", json={"name": "B", "countryCode": "NG"}, headers=b_headers)
    b = r.json()["id"]
    c_headers = await login(c_email)  # a member of A
    c_user = await _user_id(client, c_headers)
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, 'viewer')", a, c_user
    )
    a_user, b_user = await _user_id(client, a_headers), await _user_id(client, b_headers)

    # B's admin, in B's scope, sees only B's members: not A's users or their emails.
    assert _emails(as_app, b, b_user) == {b_email}
    # Even naming A's users directly finds nothing.
    with as_app.transaction(force_rollback=True):
        _in_scope(as_app, b, b_user)
        found = as_app.execute(
            "SELECT count(*) FROM app_user WHERE id = ANY(%s) OR email = ANY(%s)",
            ([uuid.UUID(a_user), uuid.UUID(c_user)], [a_email, c_email]),
        ).fetchone()
        assert found == (0,)
    # A's members see each other; a user alone sees only themself; no scope sees nobody.
    assert _emails(as_app, a, a_user) == {a_email, c_email}
    assert _emails(as_app, None, b_user) == {b_email}
    assert _emails(as_app, None, None) == set()
    # The staff console (after its checks) sees everyone.
    with as_app.transaction(force_rollback=True):
        as_app.execute("SELECT set_config('app.support_console', 'on', true)")
        everyone = {r[0] for r in as_app.execute(APP_USER_EMAILS)}
    assert {a_email, b_email, c_email} <= everyone

    # Through the API too: B's member list has no one from A.
    members = (await client.get(f"/v1/tenants/{b}/members", headers=b_headers)).json()
    assert [m["email"] for m in members] == [b_email]

    # A former member stays named in A's history (audit actor), not elsewhere.
    r = await client.patch(f"/v1/tenants/{a}", json={"name": "A2"}, headers=a_headers)
    assert r.status_code == 200, r.text
    owner_sql(
        "UPDATE membership SET role = 'admin' WHERE tenant_id = %s AND user_id = %s", a, c_user
    )
    r = await client.patch(f"/v1/tenants/{a}", json={"name": "A3"}, headers=c_headers)
    assert r.status_code == 200, r.text
    owner_sql("DELETE FROM membership WHERE tenant_id = %s AND user_id = %s", a, c_user)
    assert c_email in _emails(as_app, a, a_user)
    assert c_email not in _emails(as_app, b, b_user)


async def test_app_role_writes_only_its_own_currency_view(
    client: AsyncClient, login, as_app: psycopg.Connection
) -> None:
    a_headers, b_headers = await login(), await login()
    a_user, b_user = await _user_id(client, a_headers), await _user_id(client, b_headers)
    attempts = {
        "insert": (
            sql.SQL(
                "INSERT INTO app_user (id, email, auth_subject) VALUES (%s, 'x@example.com', 's')"
            ),
            (uuid.uuid4(),),
        ),
        "other's row": (
            sql.SQL("UPDATE app_user SET preferred_currency_view = 'usd' WHERE id = %s"),
            (b_user,),
        ),
        "own email": (
            sql.SQL("UPDATE app_user SET email = 'mine@example.com' WHERE id = %s"),
            (a_user,),
        ),
        "own row": (
            sql.SQL("UPDATE app_user SET preferred_currency_view = 'usd' WHERE id = %s"),
            (a_user,),
        ),
    }
    outcomes = {
        name: _refused(as_app, "", a_user, query, *args) for name, (query, args) in attempts.items()
    }
    assert "permission denied" in str(outcomes["insert"])
    assert outcomes["other's row"] == 0
    assert "permission denied" in str(outcomes["own email"])
    assert outcomes["own row"] == 1
    # And the API path for it still works.
    r = await client.patch("/v1/me", json={"preferredCurrencyView": "usd"}, headers=a_headers)
    assert r.status_code == 200, r.text
