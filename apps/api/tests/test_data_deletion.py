"""Data deletion (ADM-7): a disconnected connection's data, and a whole workspace.

Each test seeds one row into every workspace table for the connection or workspace being
deleted, and the same for a second connection and a second workspace that must stay.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from cloudoptics.notifications.email import Email, set_mailer
from cloudoptics.privacy.deletion import KEPT, TABLES

Sql = Callable[..., list[tuple[Any, ...]]]


class Outbox:
    def __init__(self) -> None:
        self.sent: list[Email] = []

    async def send(self, email: Email) -> None:
        self.sent.append(email)


@pytest.fixture
def outbox() -> Iterator[Outbox]:
    box = Outbox()
    set_mailer(box)
    yield box
    set_mailer(None)


def account_id() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(12))


async def workspace(client: AsyncClient, login, name: str = "Delete Me Ltd") -> tuple[str, dict]:
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": name, "countryCode": "NG"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"], headers


async def connection(client: AsyncClient, tenant: str, headers: dict, account: str) -> str:
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": account}, headers=headers
    )
    assert r.status_code == 201, r.text
    return str(r.json()["connection"]["id"])


async def member(client: AsyncClient, login, owner_sql: Sql, tenant: str, role: str) -> dict:
    headers = await login()
    me = (await client.get("/v1/me", headers=headers)).json()
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, %s)",
        tenant,
        me["id"],
        role,
    )
    return headers


def seed_connection(owner_sql: Sql, tenant: str, conn: str, account: str) -> None:
    """One row in every table that belongs to a connection."""
    tag = hashlib.sha256(conn.encode()).digest()
    batch, resource, rec = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        (
            "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, "
            "account_type) VALUES (%s, %s, %s, %s, 'standalone')",
            uuid.uuid4(),
            tenant,
            conn,
            account,
        ),
        (
            "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at) "
            "VALUES (%s, %s, %s, 'ingest', 'succeeded', now())",
            uuid.uuid4(),
            tenant,
            conn,
        ),
        (
            "INSERT INTO ingest_batch (id, tenant_id, connection_id, billing_period, "
            "export_version, manifest_key, mapping_version, status) "
            "VALUES (%s, %s, %s, '2026-10-01', 'v1', 'k', 'm1', 'succeeded')",
            batch,
            tenant,
            conn,
        ),
        (
            "INSERT INTO billing_file (id, tenant_id, connection_id, batch_id, billing_period, "
            "export_version, object_key, sha256, size_bytes, status) "
            "VALUES (%s, %s, %s, %s, '2026-10-01', 'v1', 'data/1.parquet', %s, 1, 'aggregated')",
            uuid.uuid4(),
            tenant,
            conn,
            batch,
            "0" * 64,
        ),
        (
            "INSERT INTO tag_set (tenant_id, hash, tags) VALUES (%s, %s, '{}')",
            tenant,
            tag,
        ),
        (
            "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
            "account_id, service_code, service_name, service_category, charge_category, "
            "tag_set_hash, billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) "
            "VALUES (%s, %s, '2026-10-02', '2026-10-01', %s, 'AmazonEC2', 'EC2', 'Compute', "
            "'Usage', %s, 1, 1, 1, 'v1')",
            tenant,
            conn,
            account,
            tag,
        ),
        (
            "INSERT INTO cost_resource_daily (tenant_id, connection_id, usage_date, "
            "billing_period, account_id, service_code, usage_type, resource_id, tag_set_hash, "
            "effective_cost_usd, usage_quantity) VALUES (%s, %s, '2026-10-02', '2026-10-01', "
            "%s, 'AmazonEC2', 'BoxUsage', 'i-1', %s, 1, 1)",
            tenant,
            conn,
            account,
            tag,
        ),
        (
            "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, period_start, through_date, "
            "effective_cost_usd, billed_cost_usd, credits_usd) "
            "VALUES (%s, %s, '2026-10-01', '2026-10-02', 1, 1, 0)",
            tenant,
            conn,
        ),
        (
            "INSERT INTO cost_reconciliation (tenant_id, connection_id, billing_period, "
            "export_version, through_date, status) VALUES (%s, %s, '2026-10-01', 'v1', "
            "'2026-10-02', 'ok')",
            tenant,
            conn,
        ),
        (
            "INSERT INTO resource (id, tenant_id, connection_id, account_id, region_id, "
            "resource_type, resource_id) VALUES (%s, %s, %s, %s, 'eu-west-1', 'ebs_volume', "
            "'vol-1')",
            resource,
            tenant,
            conn,
            account,
        ),
        (
            "INSERT INTO resource_metric (tenant_id, resource_id, metric, day, hourly) "
            "VALUES (%s, %s, 'cpu', '2026-10-02', array_fill(0::real, ARRAY[24]))",
            tenant,
            resource,
        ),
        (
            "INSERT INTO commitment (id, tenant_id, connection_id, kind, commitment_id, "
            "description) VALUES (%s, %s, %s, 'savings_plan', 'sp-1', 'Compute SP')",
            uuid.uuid4(),
            tenant,
            conn,
        ),
        (
            "INSERT INTO commitment_snapshot (tenant_id, connection_id) VALUES (%s, %s)",
            tenant,
            conn,
        ),
        (
            "INSERT INTO connection_intent (id, tenant_id, external_id_enc, connection_id) "
            "VALUES (%s, %s, '\\x00', %s)",
            uuid.uuid4(),
            tenant,
            conn,
        ),
        (
            "INSERT INTO recommendation (id, tenant_id, fingerprint, type, rule_id, "
            "rule_version, rule_config, generation_id, resource_ref, account_id, region_id, "
            "resource_id, title, cost_now_usd_month, cost_after_usd_month, savings_basis, "
            "effort, reversibility, risk_score, risk_level, confidence, priority_score, "
            "engine_version) VALUES (%s, %s, %s, 'R1', 'R1', 1, '{}', 1, %s, %s, 'eu-west-1', "
            "'vol-1', 'Delete volume', 10, 0, 'list_price', 'S', 'with_snapshot', 10, 'low', "
            "'high', 1, 'test')",
            rec,
            tenant,
            f"fp-{conn}",
            resource,
            account,
        ),
        (
            "INSERT INTO recommendation_event (id, tenant_id, recommendation_id, kind, body) "
            "VALUES (%s, %s, %s, 'commented', 'note')",
            uuid.uuid4(),
            tenant,
            rec,
        ),
        (
            "INSERT INTO anomaly (id, tenant_id, usage_date, account_id, service_code, "
            "service_name, actual_usd, expected_usd, mad_usd, impact_usd, engine_version) "
            "VALUES (%s, %s, '2026-10-02', %s, 'AmazonEC2', 'EC2', 10, 1, 1, 9, 'test')",
            uuid.uuid4(),
            tenant,
            account,
        ),
    ]
    for sql, *params in rows:
        owner_sql(sql, *params)


def seed_workspace(owner_sql: Sql, tenant: str) -> None:
    """One row in every workspace-wide table (membership and tenant exist already)."""
    grant = uuid.uuid4()
    rows = [
        (
            "INSERT INTO invite (id, tenant_id, email, role, expires_at) "
            "VALUES (%s, %s, %s, 'viewer', now() + interval '7 days')",
            uuid.uuid4(),
            tenant,
            f"guest-{secrets.token_hex(3)}@example.com",
        ),
        (
            "INSERT INTO budget (id, tenant_id, name, scope, amount, currency, thresholds, "
            "recipients) VALUES (%s, %s, 'Monthly', '{}', 100, 'USD', '[]', '[]')",
            uuid.uuid4(),
            tenant,
        ),
        (
            "INSERT INTO credit_balance (id, tenant_id, amount_usd, as_of) "
            "VALUES (%s, %s, 100, '2026-10-01')",
            uuid.uuid4(),
            tenant,
        ),
        ("INSERT INTO alert_setting (tenant_id) VALUES (%s)", tenant),
        ("INSERT INTO rule_setting (tenant_id) VALUES (%s)", tenant),
        ("INSERT INTO report_setting (tenant_id) VALUES (%s)", tenant),
        (
            "INSERT INTO monthly_report (tenant_id, month, content) "
            "VALUES (%s, '2026-10-01', '{}')",
            tenant,
        ),
        (
            "INSERT INTO export_job (id, tenant_id, requested_by, kind, file_name) "
            "SELECT %s, %s, m.user_id, 'costs', 'costs.csv' FROM membership m "
            "WHERE m.tenant_id = %s LIMIT 1",
            uuid.uuid4(),
            tenant,
            tenant,
        ),
        (
            "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, "
            "status) VALUES (%s, %s, 'digest', %s, '{}', 'Digest', 'sent')",
            uuid.uuid4(),
            tenant,
            secrets.token_hex(4),
        ),
        (
            "INSERT INTO notification_preference (tenant_id, kind, email) "
            "VALUES (%s, 'weekly_digest', true)",
            tenant,
        ),
        (
            "INSERT INTO aws_api_usage (tenant_id, day, api, units) "
            "VALUES (%s, '2026-10-02', 'GetMetricData', 5)",
            tenant,
        ),
        (
            "INSERT INTO support_access_grant (id, tenant_id, tenant_name, staff_user_id, "
            "justification, ticket_ref, expires_at) SELECT %s, %s, 'W', m.user_id, "
            "'Ticket 42 triage', 'SUP-42', "
            "now() + interval '1 hour' FROM membership m WHERE m.tenant_id = %s LIMIT 1",
            grant,
            tenant,
            tenant,
        ),
        (
            "INSERT INTO support_access_request (grant_id, tenant_id, method, path, allowed) "
            "VALUES (%s, %s, 'GET', '/v1/tenants', true)",
            grant,
            tenant,
        ),
    ]
    for sql, *params in rows:
        owner_sql(sql, *params)


def tenant_rows(owner_sql: Sql, tenant: str) -> dict[str, int]:
    """Rows per table, counted as the superuser (no RLS)."""
    counts: dict[str, int] = {}
    for name, spec in TABLES.items():
        query = f"SELECT count(*) FROM {name} WHERE {spec.tenant_column} = %s"  # noqa: S608
        counts[name] = int(owner_sql(query, tenant)[0][0])
    return counts


def connection_rows(owner_sql: Sql, conn: str) -> dict[str, int]:
    one = "SELECT count(*) FROM {} WHERE connection_id = %s"
    counts = {
        name: int(owner_sql(one.format(name), conn)[0][0])
        for name, spec in TABLES.items()
        if spec.connection == "connection_id"
    }
    counts["cloud_connection"] = int(
        owner_sql("SELECT count(*) FROM cloud_connection WHERE id = %s", conn)[0][0]
    )
    return counts


async def disconnect(client: AsyncClient, tenant: str, headers: dict, conn: str) -> None:
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{conn}", headers=headers)
    assert r.status_code == 204, r.text


# --- Coverage: every workspace table is known to the deletion code ---------------------


def test_every_tenant_table_is_covered_by_the_deletion_code(owner_sql: Sql) -> None:
    with_tenant = {
        r[0]
        for r in owner_sql(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name = 'tenant_id'"
        )
    }
    known = (set(TABLES) - {"tenant"}) | set(KEPT)
    assert with_tenant - known == set(), "add new tenant tables to privacy.deletion.TABLES"
    assert known - with_tenant == set(), "TABLES lists a table without tenant_id"

    # Each table goes with its workspace: tenant_id references tenant ON DELETE CASCADE.
    cascades = {
        r[0]
        for r in owner_sql(
            "SELECT c.conrelid::regclass::text FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' AND c.confrelid = 'tenant'::regclass "
            "AND c.confdeltype = 'c' AND a.attname = 'tenant_id'"
        )
    }
    assert set(TABLES) - {"tenant"} - cascades == set()

    # Every table with a connection_id has a connection scope, and goes with the connection.
    with_connection = {
        r[0]
        for r in owner_sql(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name = 'connection_id'"
        )
    }
    scoped = {n for n, s in TABLES.items() if s.connection == "connection_id"}
    assert with_connection - set(KEPT) == scoped
    connection_cascades = {
        r[0]
        for r in owner_sql(
            "SELECT conrelid::regclass::text FROM pg_constraint WHERE contype = 'f' "
            "AND confrelid = 'cloud_connection'::regclass AND confdeltype = 'c'"
        )
    }
    assert scoped - connection_cascades == set()


# --- Connection data ------------------------------------------------------------------


async def test_deletes_a_disconnected_connections_data_and_nothing_else(
    client: AsyncClient, login, owner_sql: Sql, outbox: Outbox
) -> None:
    tenant, admin = await workspace(client, login)
    gone_account, kept_account = account_id(), account_id()
    gone = await connection(client, tenant, admin, gone_account)
    kept = await connection(client, tenant, admin, kept_account)
    seed_connection(owner_sql, tenant, gone, gone_account)
    seed_connection(owner_sql, tenant, kept, kept_account)
    seed_workspace(owner_sql, tenant)
    other, other_admin = await workspace(client, login, "Bystander")
    other_account = account_id()
    other_conn = await connection(client, other, other_admin, other_account)
    seed_connection(owner_sql, other, other_conn, other_account)
    other_before = tenant_rows(owner_sql, other)
    kept_before = connection_rows(owner_sql, kept)
    workspace_before = tenant_rows(owner_sql, tenant)

    await disconnect(client, tenant, admin, gone)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/{gone}/delete-data",
        json={"confirm": gone_account},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["kind"] == "connection"
    assert body["targetLabel"] == gone_account
    tables = body["verification"]["tables"]
    assert body["verification"]["verified"] is True
    assert set(tables) == {n for n, s in TABLES.items() if s.connection is not None}
    assert all(t["before"] >= 1 and t["after"] == 0 for t in tables.values()), tables
    assert body["verification"]["unusedTagSetsDeleted"] == 1

    # Gone as seen by the superuser too, not only through RLS.
    assert all(n == 0 for n in connection_rows(owner_sql, gone).values())
    assert owner_sql("SELECT count(*) FROM anomaly WHERE account_id = %s", gone_account) == [(0,)]
    assert owner_sql("SELECT count(*) FROM recommendation WHERE account_id = %s", gone_account) == [
        (0,)
    ]
    # The other connection, the workspace-wide rows and the other workspace are untouched.
    assert connection_rows(owner_sql, kept) == kept_before
    assert tenant_rows(owner_sql, other) == other_before
    after = tenant_rows(owner_sql, tenant)
    for name, spec in TABLES.items():
        if spec.connection is None and name not in ("tag_set", "notification"):
            assert after[name] == workspace_before[name], name
    assert after["tag_set"] == workspace_before["tag_set"] - 1
    # The confirmation email is logged.
    assert after["notification"] == workspace_before["notification"] + 1

    listed = await client.get(f"/v1/tenants/{tenant}/connections", headers=admin)
    assert [c["id"] for c in listed.json()] == [kept]
    history = (await client.get(f"/v1/tenants/{tenant}/deletions", headers=admin)).json()
    assert [h["id"] for h in history] == [body["id"]]

    log = (await client.get(f"/v1/tenants/{tenant}/audit-log", headers=admin)).json()
    entry = next(e for e in log["entries"] if e["action"] == "connection.data_deleted")
    assert entry["actor"]["type"] == "user"
    assert entry["after"]["tables"] == tables
    assert log["verified"] is True
    assert [e.subject for e in outbox.sent] == [f"Data for AWS account {gone_account} deleted"]


async def test_connection_data_needs_a_disconnect_and_the_typed_account_id(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    tenant, admin = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, admin, account)
    url = f"/v1/tenants/{tenant}/connections/{conn}/delete-data"

    r = await client.post(url, json={"confirm": account}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "connection_not_disconnected"
    await disconnect(client, tenant, admin, conn)
    r = await client.post(url, json={"confirm": "000000000000"}, headers=admin)
    assert r.status_code == 422 and r.json()["code"] == "confirmation_mismatch"
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE id = %s", conn) == [(1,)]
    missing = f"/v1/tenants/{tenant}/connections/{uuid.uuid4()}/delete-data"
    assert (await client.post(missing, json={"confirm": account}, headers=admin)).status_code == 404


async def test_only_admins_of_the_workspace_delete_connection_data(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    tenant, admin = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, admin, account)
    await disconnect(client, tenant, admin, conn)
    url = f"/v1/tenants/{tenant}/connections/{conn}/delete-data"
    for role in ("member", "viewer"):
        headers = await member(client, login, owner_sql, tenant, role)
        r = await client.post(url, json={"confirm": account}, headers=headers)
        assert r.status_code == 403
        assert (
            await client.get(f"/v1/tenants/{tenant}/deletions", headers=headers)
        ).status_code == 403
    # An admin of another workspace cannot even see it.
    _, stranger = await workspace(client, login, "Elsewhere")
    assert (await client.post(url, json={"confirm": account}, headers=stranger)).status_code == 404
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE id = %s", conn) == [(1,)]


async def test_a_queued_connection_deletion_is_dropped_if_the_account_was_reconnected(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    from cloudoptics.privacy import jobs

    tenant, admin = await workspace(client, login)
    account = account_id()
    conn = await connection(client, tenant, admin, account)
    deletion = uuid.uuid4()
    owner_sql(
        "INSERT INTO data_deletion (id, tenant_id, kind, connection_id, target_label, "
        "scheduled_for) VALUES (%s, %s, 'connection', %s, %s, now())",
        deletion,
        tenant,
        conn,
        account,
    )
    # Still connected (pending): the job leaves the data alone.
    assert await jobs.execute(uuid.UUID(tenant), deletion, datetime.now(UTC)) == "cancelled"
    assert owner_sql("SELECT status FROM data_deletion WHERE id = %s", deletion) == [("cancelled",)]
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE id = %s", conn) == [(1,)]
    assert await jobs.execute(uuid.UUID(tenant), deletion, datetime.now(UTC)) == "skipped"


# --- Workspace ------------------------------------------------------------------------


async def test_workspace_deletion_waits_for_the_grace_period_and_can_be_cancelled(
    client: AsyncClient, login, owner_sql: Sql, outbox: Outbox
) -> None:
    from cloudoptics.privacy import jobs

    tenant, admin = await workspace(client, login, "Grace Period Co")
    account = account_id()
    await connection(client, tenant, admin, account)
    url = f"/v1/tenants/{tenant}/deletion"
    assert (await client.get(url, headers=admin)).json()["deletion"] is None

    r = await client.post(url, json={"confirmName": "grace period co"}, headers=admin)
    assert r.status_code == 422 and r.json()["code"] == "confirmation_mismatch"
    r = await client.post(url, json={"confirmName": "Grace Period Co"}, headers=admin)
    assert r.status_code == 202, r.text
    body = r.json()
    scheduled = datetime.fromisoformat(body["deletion"]["scheduledFor"])
    assert timedelta(days=6, hours=23) < scheduled - datetime.now(UTC) <= timedelta(days=7)
    assert body["graceDays"] == 7
    assert body["stacks"] == [
        {
            "awsAccountId": account,
            "accountType": "standalone",
            "stacks": ["cloudoptics-billing-access", "cloudoptics-resource-access"],
        }
    ]
    tenant_view = (await client.get(f"/v1/tenants/{tenant}", headers=admin)).json()
    assert tenant_view["status"] == "deleting"
    again = await client.post(url, json={"confirmName": "Grace Period Co"}, headers=admin)
    assert again.status_code == 409
    [scheduled_mail] = outbox.sent
    assert scheduled_mail.subject.startswith("Workspace Grace Period Co will be deleted on")
    assert account in scheduled_mail.text and "cloudoptics-billing-access" in scheduled_mail.text

    # Not due yet: the poll finds nothing and running it early does nothing.
    assert (tenant, body["deletion"]["id"]) not in await jobs.due()
    deletion = uuid.UUID(body["deletion"]["id"])
    assert await jobs.execute(uuid.UUID(tenant), deletion, datetime.now(UTC)) == "not_due"

    assert (await client.delete(url, headers=admin)).status_code == 204
    assert (await client.get(url, headers=admin)).json()["deletion"] is None
    assert (await client.get(f"/v1/tenants/{tenant}", headers=admin)).json()["status"] == "active"
    assert (await client.delete(url, headers=admin)).status_code == 404
    later = datetime.now(UTC) + timedelta(days=8)
    assert await jobs.execute(uuid.UUID(tenant), deletion, later) == "skipped"
    assert owner_sql("SELECT count(*) FROM tenant WHERE id = %s", tenant) == [(1,)]
    assert outbox.sent[-1].subject == "Deletion of workspace Grace Period Co cancelled"
    actions = [
        e["action"]
        for e in (await client.get(f"/v1/tenants/{tenant}/audit-log", headers=admin)).json()[
            "entries"
        ]
    ]
    assert actions[:2] == ["tenant.deletion_cancelled", "tenant.deletion_scheduled"]


async def test_workspace_deletion_removes_every_table_and_keeps_the_audit_log(
    client: AsyncClient, login, owner_sql: Sql, outbox: Outbox
) -> None:
    from cloudoptics.platform.db import get_sessionmaker, scope
    from cloudoptics.privacy import jobs

    tenant, admin = await workspace(client, login, "Closing Down")
    account = account_id()
    conn = await connection(client, tenant, admin, account)
    seed_connection(owner_sql, tenant, conn, account)
    seed_workspace(owner_sql, tenant)
    viewer = await member(client, login, owner_sql, tenant, "viewer")
    other, other_admin = await workspace(client, login, "Still Here")
    other_account = account_id()
    other_conn = await connection(client, other, other_admin, other_account)
    seed_connection(owner_sql, other, other_conn, other_account)
    seed_workspace(owner_sql, other)
    other_before = tenant_rows(owner_sql, other)
    assert all(n >= 1 for n in tenant_rows(owner_sql, tenant).values())

    r = await client.post(
        f"/v1/tenants/{tenant}/deletion", json={"confirmName": "Closing Down"}, headers=admin
    )
    assert r.status_code == 202, r.text
    deletion = r.json()["deletion"]["id"]
    owner_sql(
        "UPDATE data_deletion SET scheduled_for = now() - interval '1 minute' WHERE id = %s",
        deletion,
    )
    assert (tenant, deletion) in await jobs.due()
    audit_before = owner_sql("SELECT count(*) FROM audit_log WHERE tenant_id = %s", tenant)[0][0]

    assert await jobs.execute(uuid.UUID(tenant), uuid.UUID(deletion), datetime.now(UTC)) == (
        "completed"
    )

    assert all(n == 0 for n in tenant_rows(owner_sql, tenant).values())
    assert tenant_rows(owner_sql, other) == other_before
    record = owner_sql("SELECT status, verification FROM data_deletion WHERE id = %s", deletion)[0]
    assert record[0] == "completed"
    verification = record[1]
    assert set(verification["tables"]) == set(TABLES)
    assert all(t["before"] >= 1 and t["after"] == 0 for t in verification["tables"].values())
    assert verification["kept"]["audit_log"] == audit_before

    # The audit log stays, with the deletion as its last entry, and its chain still verifies.
    last = owner_sql(
        "SELECT action, actor_type FROM audit_log WHERE tenant_id = %s ORDER BY id DESC LIMIT 1",
        tenant,
    )
    assert last == [("tenant.data_deleted", "system")]
    from cloudoptics.audit.log import verify_chain

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tenant))
        assert await verify_chain(session, uuid.UUID(tenant)) is True

    # Former members no longer see it; the deletion email went to its admins.
    assert (await client.get(f"/v1/tenants/{tenant}", headers=admin)).status_code == 404
    assert (await client.get(f"/v1/tenants/{tenant}", headers=viewer)).status_code == 404
    me = (await client.get("/v1/me", headers=admin)).json()
    assert tenant not in [m["tenantId"] for m in me["memberships"]]
    done = outbox.sent[-1]
    assert done.subject == "Workspace Closing Down deleted"
    assert "cloudoptics-billing-access" in done.text
    assert await jobs.execute(uuid.UUID(tenant), uuid.UUID(deletion), datetime.now(UTC)) == (
        "skipped"
    )


async def test_only_admins_schedule_or_cancel_a_workspace_deletion(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    tenant, admin = await workspace(client, login, "Guarded")
    url = f"/v1/tenants/{tenant}/deletion"
    for role in ("member", "viewer"):
        headers = await member(client, login, owner_sql, tenant, role)
        assert (
            await client.post(url, json={"confirmName": "Guarded"}, headers=headers)
        ).status_code == 403
        assert (await client.delete(url, headers=headers)).status_code == 403
    _, stranger = await workspace(client, login, "Guarded")
    assert (
        await client.post(url, json={"confirmName": "Guarded"}, headers=stranger)
    ).status_code == 404
    assert (await client.get(url, headers=admin)).json()["deletion"] is None
