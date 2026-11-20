"""The Google Cloud plug-in (ADR-019) against the fake Google Cloud (tests/fake_gcp.py):
federation, one service account per connection (created, impersonated, deleted), the connect
flow and its checks, FOCUS loads in USD and other currencies, reloads, reconciliation, project
discovery, inventory, Recommender, metrics and rules."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import pytest
from fake_gcp import (
    BILLING_ACCOUNT,
    DATASET,
    EXPORT_PROJECT,
    FOCUS_FIELDS,
    PROJECTS,
    SA_PROJECT,
    TABLE,
    WORKLOAD_PROVIDER,
    FakeGcp,
    focus_rows,
    install,
    uninstall,
)
from httpx import AsyncClient
from test_data_deletion import workspace

from cloudoptics import providers
from cloudoptics.analysis import engine
from cloudoptics.analysis.pricing import PriceBook
from cloudoptics.analysis.rules import Context, Resource
from cloudoptics.ingestion import jobs as ingestion_jobs
from cloudoptics.providers.gcp import billing, client, connect, identity, inventory
from cloudoptics.providers.gcp.client import AwsCredentials, decode_rows, signed_caller_identity
from cloudoptics.providers.gcp.rules import GCP_RULES, machine_month_usd

Sql = Callable[..., list[tuple[Any, ...]]]
NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
VIEW_SQL = f"/* co:none */ SELECT 1 FROM `{EXPORT_PROJECT}.attacker.focus`"  # noqa: S608
SA_EMAIL = re.compile(rf"^co-[0-9a-f]{{12}}@{SA_PROJECT}\.iam\.gserviceaccount\.com$")


@pytest.fixture
def gcp() -> Iterator[FakeGcp]:
    fake = FakeGcp().bill().grant_all()
    install(fake)
    yield fake
    uninstall()


def create_body(fake: FakeGcp, **extra: Any) -> dict[str, Any]:
    return {
        "billingAccountId": fake.billing_account.lower(),
        "exportProjectId": EXPORT_PROJECT,
        "exportDatasetId": DATASET,
        "exportTableId": TABLE,
        "exportKind": "focus_export",
        **extra,
    }


async def gcp_connection(
    client_: AsyncClient,
    tenant: str,
    headers: dict[str, str],
    fake: FakeGcp,
    *,
    share: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    r = await client_.post(
        f"/v1/tenants/{tenant}/connections/gcp", json=create_body(fake, **extra), headers=headers
    )
    assert r.status_code == 201, r.text
    setup: dict[str, Any] = r.json()
    if share:
        fake.customer_setup(setup["serviceAccount"])
    return setup


async def validate(client_: AsyncClient, tenant: str, headers: dict[str, str], conn: str) -> Any:
    r = await client_.post(f"/v1/tenants/{tenant}/connections/{conn}/validate", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def connected(client_: AsyncClient, login, fake: FakeGcp) -> tuple[str, dict[str, str], str]:
    tenant, headers = await workspace(client_, login, "Acme Cloud Ltd")
    setup = await gcp_connection(client_, tenant, headers, fake)
    conn = setup["connection"]["id"]
    assert (await validate(client_, tenant, headers, conn))["status"] == "healthy"
    return tenant, headers, conn


def settings_row(query_project: str | None = None, sa: str | None = None) -> connect.GcpSettingsRow:
    return connect.GcpSettingsRow(
        uuid.uuid4(), BILLING_ACCOUNT, EXPORT_PROJECT, DATASET, TABLE, "focus_export",
        query_project, "EU", sa,
    )  # fmt: skip


# --- units ------------------------------------------------------------------------------


def test_the_subject_token_is_a_signed_get_caller_identity() -> None:
    token = signed_caller_identity(
        AwsCredentials("AKIDEXAMPLE", "secret", "session"), WORKLOAD_PROVIDER, "us-east-1"
    )
    request = json.loads(unquote(token))
    assert request["method"] == "POST"
    assert request["url"] == (
        "https://sts.us-east-1.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
    )
    headers = {h["key"].lower(): h["value"] for h in request["headers"]}
    assert headers["x-goog-cloud-target-resource"] == WORKLOAD_PROVIDER
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "x-goog-cloud-target-resource" in headers["authorization"]  # signed, not just sent
    assert headers["x-amz-security-token"] == "session"
    assert "secret" not in token


def test_the_plug_in_is_registered() -> None:
    gcp = providers.get("gcp")
    assert gcp.label == "Google Cloud" and gcp.account_noun == "project"
    assert gcp.valid_account_id("acme-shop-prod") and not gcp.valid_account_id("Acme")
    assert gcp.billing_currencies is None and gcp.accepts_currency("NGN")
    for name in ("connect", "credentials", "accounts", "billing_source", "reconcile",
                 "inventory", "metrics", "native_recs"):  # fmt: skip
        assert gcp.capability(name) is not None, name
    assert gcp.commitments is None and gcp.prices is None
    assert len(gcp.rules) >= 5 and all(r.provider == "gcp" for r in gcp.rules)
    assert "gce_instance" in gcp.resource_types


def test_periods_tags_versions_prices_and_rows() -> None:
    nov = date(2026, 11, 1)
    assert billing.periods_to_read(date(2026, 11, 19), set(), 2) == [
        date(2026, 9, 1), date(2026, 10, 1), nov,
    ]  # fmt: skip
    loaded = {date(2026, 9, 1), date(2026, 10, 1)}
    assert billing.periods_to_read(date(2026, 11, 19), loaded, 2) == [date(2026, 10, 1), nov]
    assert billing.add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert billing.parse_tags('{"env":"prod"}') == [("env", "prod")]
    assert billing.parse_tags('[{"key":"team","value":"core"}]') == [("team", "core")]
    assert billing.parse_tags('[{"Key":"env","Value":"prod"}]') == [("env", "prod")]  # x_Labels
    assert billing.parse_tags(None) == [] and billing.parse_tags("null") == []
    rows = [{"a": 1}, {"b": Decimal("2.5")}]
    assert billing.version_of(rows) == billing.version_of(list(reversed(rows)))
    assert machine_month_usd("e2-standard-4") == pytest.approx(97.83, abs=0.05)
    assert machine_month_usd("e2-micro") == 6.11
    assert machine_month_usd("n2-custom-4-8192") is not None
    assert machine_month_usd("a3-megagpu-8g") is None
    decoded = decode_rows(
        [
            {"name": "d", "type": "DATE"},
            {"name": "n", "type": "NUMERIC"},
            {"name": "i", "type": "INTEGER"},
        ],
        [{"f": [{"v": "2026-11-01"}, {"v": "1.5"}, {"v": None}]}],
    )
    assert decoded == [{"d": nov, "n": Decimal("1.5"), "i": None}]


def test_account_ids_links_roles_and_commands(gcp: FakeGcp) -> None:
    for _ in range(50):  # Google's rule: 6-30 characters, [a-z]([-a-z0-9]*[a-z0-9])
        account_id = identity.new_account_id()
        assert 6 <= len(account_id) <= 30
        assert re.fullmatch(identity.ACCOUNT_ID_PATTERN, account_id)
    sa = identity.email_for("co-0123456789ab", SA_PROJECT)
    ours = settings_row(sa=sa)
    assert not ours.customer_pays and ours.query_project == SA_PROJECT
    assert "roles/bigquery.jobUser" not in connect.access_roles(ours)
    links = connect.links(ours)
    assert links["billing_export"] == "https://console.cloud.google.com/billing/export"
    assert links["dataset"] == (
        f"https://console.cloud.google.com/bigquery?project={EXPORT_PROJECT}&p={EXPORT_PROJECT}"
        f"&d={DATASET}&page=dataset"
    )
    assert links["iam_project"].endswith(f"/iam-admin/iam?project={EXPORT_PROJECT}")
    assert "supportedpurview=project%2Cfolder%2CorganizationId" in links["iam_any"]
    cmds = {c.id: c for c in connect.commands(ours, service_account=sa, projects=[])}
    assert set(cmds) == {"dataset", "projects"}  # no Job User: queries run in our project
    assert "GRANT `roles/bigquery.dataViewer` ON SCHEMA" in cmds["dataset"].command
    assert f'TO "serviceAccount:{sa}"' in cmds["dataset"].command
    assert "--location=EU" in cmds["dataset"].command
    assert cmds["projects"].optional and EXPORT_PROJECT in cmds["projects"].command

    theirs = settings_row("acme-queries", sa)
    assert theirs.customer_pays and theirs.query_project == "acme-queries"
    assert connect.access_roles(theirs)[0] == "roles/bigquery.jobUser"
    jobs = {c.id: c for c in connect.commands(theirs, service_account=sa, projects=[])}["jobs"]
    assert "gcloud projects add-iam-policy-binding acme-queries" in jobs.command
    with pytest.raises(client.NoServiceAccountError):
        connect.api_for(settings_row())


# --- connect ----------------------------------------------------------------------------


async def test_connect_makes_an_account_and_checks_each_step(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, headers = await workspace(client, login, "Acme Cloud Ltd")
    setup = await gcp_connection(client, tenant, headers, gcp, share=False)
    conn = setup["connection"]
    sa = setup["serviceAccount"]
    assert SA_EMAIL.match(sa) and sa in gcp.service_accounts  # made in our project
    assert conn["provider"] == "gcp" and conn["awsAccountId"] == gcp.billing_account
    assert conn["accountType"] == "management" and conn["status"] == "pending"
    assert conn["roleArn"] == sa and setup["enabled"] is True
    assert setup["customerPaysQueries"] is False
    assert setup["roles"][0]["title"] == "Compute Viewer"  # no Job User
    assert setup["links"]["dataset"].endswith(f"&d={DATASET}&page=dataset")
    assert [c["id"] for c in setup["commands"]] == ["dataset", "projects"]
    assert setup["settings"]["queryProjectId"] is None
    assert "labelValue" not in setup
    created = owner_sql(
        "SELECT after->>'serviceAccount' FROM audit_log WHERE tenant_id = %s "
        "AND action = 'gcp.service_account_created'",
        tenant,
    )
    assert created == [(sa,)]

    # Not shared yet: the check says which step is missing, and nothing is read.
    body = await validate(client, tenant, headers, conn["id"])
    assert body["status"] == "failed" and body["lastError"]["check"] == "dataset"
    dataset = next(c for c in body["permissionChecks"] if c["id"] == "dataset")
    assert dataset["message"].endswith("finish step 2.") and sa in dataset["hint"]
    assert not any("queries" in c for c in gcp.calls)

    gcp.customer_setup(sa)
    body = await validate(client, tenant, headers, conn["id"])
    assert body["status"] == "healthy", body["permissionChecks"]
    checks = {c["id"]: c["status"] for c in body["permissionChecks"]}
    assert checks == {
        "federation": "ok", "dataset": "ok", "export_table": "ok", "query_jobs": "ok",
        "billing_account": "ok", "projects": "skipped",
    }  # fmt: skip
    # Every read was made as this connection's account; queries ran in our project.
    assert {who for _, who in gcp.callers} == {sa}
    assert any(f"projects/{SA_PROJECT}/queries" in c for c in gcp.calls)
    verified = owner_sql(
        "SELECT account_verified_at IS NOT NULL FROM cloud_connection WHERE id = %s", conn["id"]
    )
    assert verified == [(True,)]
    accounts = owner_sql(
        "SELECT provider, provider_account_id, account_type FROM cloud_account "
        "WHERE connection_id = %s",
        conn["id"],
    )
    assert accounts == [("gcp", gcp.billing_account, "management")]
    cost = owner_sql(
        "SELECT max(est_customer_api_cost_usd) FROM sync_run WHERE connection_id = %s",
        conn["id"],
    )
    assert float(cost[0][0] or 0) == 0.0  # Cloudoptics pays for its queries

    # The setup page again keeps the account; a second connect of the same billing account.
    again = await client.get(f"/v1/tenants/{tenant}/connections/gcp/{conn['id']}", headers=headers)
    assert again.status_code == 200 and again.json()["serviceAccount"] == sa
    assert len(gcp.service_accounts) == 1
    dup = await client.post(
        f"/v1/tenants/{tenant}/connections/gcp", json=create_body(gcp), headers=headers
    )
    assert dup.status_code == 409

    # Another workspace cannot claim the proven billing account, even when the customer
    # shares the same export with its account too.
    other, other_headers = await workspace(client, login, "Copycat Ltd")
    theirs = await gcp_connection(client, other, other_headers, gcp)
    assert theirs["serviceAccount"] != sa
    body = await validate(client, other, other_headers, theirs["connection"]["id"])
    assert body["status"] == "failed" and body["lastError"]["code"] == "AccountInUse"


async def test_another_workspace_cannot_read_a_dataset_shared_with_someone_else(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    """The property that replaced the dataset label: workspace B types workspace A's names,
    but its own account was never given A's data, so it reads nothing."""
    tenant_a, _, conn_a = await connected(client, login, gcp)
    sa_a = owner_sql("SELECT role_arn FROM cloud_connection WHERE id = %s", conn_a)[0][0]

    tenant_b, headers_b = await workspace(client, login, "Attacker Ltd")
    setup_b = await gcp_connection(client, tenant_b, headers_b, gcp, share=False)
    sa_b, conn_b = setup_b["serviceAccount"], setup_b["connection"]["id"]
    assert sa_b != sa_a
    gcp.calls.clear()
    gcp.callers.clear()
    body = await validate(client, tenant_b, headers_b, conn_b)
    assert body["status"] == "failed" and body["lastError"] == {
        "code": "accessDenied",
        "check": "dataset",
    }
    assert {who for _, who in gcp.callers} == {sa_b}  # never A's account
    assert not any("/queries" in c for c in gcp.calls)

    # Straight at the API as B's account: the dataset, the table and a query are all refused.
    row = await _settings(owner_sql, conn_b)
    api = connect.api_for(row)
    for call in (
        api.dataset(EXPORT_PROJECT, DATASET),
        api.table(EXPORT_PROJECT, DATASET, TABLE),
        api.query(SA_PROJECT, billing.daily_sql(row, set(FOCUS_FIELDS)), []),
    ):
        with pytest.raises(client_mod().GcpError) as err:
            await call
        assert err.value.denied
    # A view of B's own over A's export reads nothing either: BigQuery runs it with B's access.
    gcp.datasets.add((EXPORT_PROJECT, "attacker"))
    gcp.views[(EXPORT_PROJECT, "attacker", "focus")] = [(EXPORT_PROJECT, DATASET, TABLE)]
    gcp.share(sa_b, EXPORT_PROJECT, "attacker")
    with pytest.raises(client_mod().GcpError) as err:
        await api.query(SA_PROJECT, VIEW_SQL)
    assert err.value.denied
    assert (await billing.ingest(uuid.UUID(tenant_b), uuid.UUID(conn_b))).status == "skipped"
    rows = owner_sql("SELECT count(*) FROM cost_daily WHERE connection_id = %s", conn_b)
    assert rows == [(0,)]
    assert tenant_a != tenant_b


async def _settings(owner_sql: Sql, conn: str) -> connect.GcpSettingsRow:
    [r] = owner_sql(
        "SELECT connection_id, billing_account_id, export_project_id, export_dataset_id, "
        "export_table_id, export_kind, query_project_id, dataset_location, "
        "service_account_email FROM gcp_connection WHERE connection_id = %s",
        conn,
    )
    return connect.GcpSettingsRow(*r)


async def test_a_view_needs_each_dataset_it_reads_shared(
    client: AsyncClient, login, gcp: FakeGcp
) -> None:
    """The detailed export plus Google's FOCUS view: the view's dataset and the datasets it
    reads must all be shared with the account (step 2 says so)."""
    source = ("acme-billing", "detailed_export", "gcp_billing_export_resource_v1_x")
    gcp.views[(EXPORT_PROJECT, DATASET, TABLE)] = [source]
    gcp.datasets.add(source[:2])
    tenant, headers = await workspace(client, login, "Acme Views Ltd")
    setup = await gcp_connection(client, tenant, headers, gcp, exportKind="focus_view")
    conn, sa = setup["connection"]["id"], setup["serviceAccount"]
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "degraded" and body["lastError"]["check"] == "billing_account"
    check = next(c for c in body["permissionChecks"] if c["id"] == "billing_account")
    assert "finish step 2" in check["message"]
    assert not any("co:daily" in c for c in gcp.calls)
    gcp.share(sa, *source[:2])
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "healthy", body["permissionChecks"]


async def test_connect_failures_are_specific(client: AsyncClient, login, gcp: FakeGcp) -> None:
    tenant, headers = await workspace(client, login)
    bad = await client.post(
        f"/v1/tenants/{tenant}/connections/gcp",
        json={**create_body(gcp), "billingAccountId": "nope"},
        headers=headers,
    )
    assert bad.status_code == 422
    # The customer's own query project: then step 3 includes BigQuery Job User.
    setup = await gcp_connection(client, tenant, headers, gcp, queryProjectId=EXPORT_PROJECT)
    assert setup["customerPaysQueries"] is True
    assert setup["roles"][0] == {"role": "roles/bigquery.jobUser", "title": "BigQuery Job User"}
    conn, sa = setup["connection"]["id"], setup["serviceAccount"]

    gcp.tables[(EXPORT_PROJECT, DATASET, TABLE)] = ("BilledCost",)
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "degraded" and body["lastError"]["code"] == "NotFocus"

    gcp.tables[(EXPORT_PROJECT, DATASET, TABLE)] = FOCUS_FIELDS
    gcp.members[EXPORT_PROJECT].discard(sa)
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "degraded"
    jobs = next(c for c in body["permissionChecks"] if c["id"] == "query_jobs")
    assert jobs["status"] == "error" and jobs["message"].endswith("finish step 3.")

    gcp.grant(sa)
    gcp.rows = {p: focus_rows(p, billing_account="AAAAAA-BBBBBB-CCCCCC") for p in gcp.rows}
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "failed" and body["lastError"]["code"] == "BillingAccountMismatch"
    assert "check step 1" in body["permissionChecks"][-2]["message"]

    gcp.unshare(sa)
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "failed" and body["lastError"]["check"] == "dataset"

    gcp.sts_ok = False
    client_module = client_mod()
    client_module.reset_token()
    body = await validate(client, tenant, headers, conn)
    assert body["status"] == "failed" and body["lastError"]["check"] == "federation"


def client_mod() -> Any:
    from cloudoptics.providers.gcp import client as gcp_client

    return gcp_client


async def test_the_account_quota_fails_clearly(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, headers = await workspace(client, login)
    gcp.sa_quota = len(gcp.service_accounts)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/gcp", json=create_body(gcp), headers=headers
    )
    assert r.status_code == 503, r.text
    assert "limit for connection accounts" in r.text
    left = owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant)
    assert left == [(0,)]  # nothing half-made
    gcp.sa_quota = 100
    await gcp_connection(client, tenant, headers, gcp)


async def test_disconnect_deletes_the_account_and_reconnect_makes_a_new_one(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, headers, conn = await connected(client, login, gcp)
    sa = owner_sql("SELECT role_arn FROM cloud_connection WHERE id = %s", conn)[0][0]
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{conn}", headers=headers)
    assert r.status_code == 204
    assert sa not in gcp.service_accounts
    state = owner_sql(
        "SELECT service_account_state FROM gcp_connection WHERE connection_id = %s", conn
    )
    assert state == [("deleted",)]
    actions = [
        a
        for (a,) in owner_sql(
            "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant
        )
    ]
    assert "gcp.service_account_deleted" in actions
    # Its token no longer works anywhere.
    client_mod().reset_token()
    with pytest.raises(client_mod().GcpError):
        await client_mod().GcpApi(service_account=sa).dataset(EXPORT_PROJECT, DATASET)

    # Connecting again keeps the row and its history, with a new account to share with.
    setup = await gcp_connection(client, tenant, headers, gcp)
    assert setup["connection"]["id"] == conn and setup["serviceAccount"] != sa
    assert (await validate(client, tenant, headers, conn))["status"] == "healthy"

    # Google down at disconnect: the account is kept for deletion and retried later.
    gcp.failures["iam.googleapis.com"] = (503, "UNAVAILABLE")
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{conn}", headers=headers)
    assert r.status_code == 204
    pending = owner_sql(
        "SELECT service_account_state FROM gcp_connection WHERE connection_id = %s", conn
    )
    assert pending == [("delete_pending",)]
    gcp.failures.clear()
    outcome = await _retire(tenant, conn)
    assert outcome == "deleted" and setup["serviceAccount"] not in gcp.service_accounts


async def _retire(tenant: str, conn: str) -> str:
    from cloudoptics.audit.log import Actor
    from cloudoptics.platform.db import get_sessionmaker, scope

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tenant))
        return await identity.retire(
            session, uuid.UUID(tenant), uuid.UUID(conn), Actor(type="system")
        )


async def test_not_switched_on(client: AsyncClient, login, gcp: FakeGcp) -> None:
    tenant, headers = await workspace(client, login)
    uninstall()
    try:
        r = await client.post(
            f"/v1/tenants/{tenant}/connections/gcp", json=create_body(gcp), headers=headers
        )
        assert r.status_code == 201 and r.json()["enabled"] is False
        assert r.json()["serviceAccount"] == "" and r.json()["commands"] == []
        conn = r.json()["connection"]["id"]
        body = await validate(client, tenant, headers, conn)
        assert body["lastError"]["code"] == "GcpNotConfigured"
    finally:
        install(gcp)
    # Switched on later: the setup page makes the account.
    again = await client.get(f"/v1/tenants/{tenant}/connections/gcp/{conn}", headers=headers)
    assert SA_EMAIL.match(again.json()["serviceAccount"])


async def test_viewers_cannot_connect(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    from test_data_deletion import member

    tenant, _ = await workspace(client, login)
    viewer = await member(client, login, owner_sql, tenant, "viewer")
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/gcp", json=create_body(gcp), headers=viewer
    )
    assert r.status_code == 403
    assert gcp.service_accounts == {}


# --- bills ------------------------------------------------------------------------------


async def test_bills_load_into_the_shared_cost_tables(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, _, conn = await connected(client, login, gcp)
    result = await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
    assert result.status == "succeeded", result.errors
    assert sorted(result.loaded_periods) == sorted(gcp.rows)

    rows = owner_sql(
        "SELECT provider, billing_period, provider_account_id, charge_category, "
        "sum(billed_cost_usd), sum(billed_cost), min(billing_currency) FROM cost_daily "
        "WHERE connection_id = %s GROUP BY 1, 2, 3, 4 ORDER BY 2, 3, 4",
        conn,
    )
    assert {r[0] for r in rows} == {"gcp"}
    accounts = {r[2] for r in rows}
    assert accounts == {gcp.billing_account, *PROJECTS}  # the tax line has no project
    for period, period_rows in gcp.rows.items():
        expected = sum(r["BilledCost"] for r in period_rows)
        loaded = sum(r[4] for r in rows if r[1] == period)
        assert loaded == pytest.approx(expected)
    # Google's export has no Credit rows: credits are inside BilledCost (cost + credits).
    assert {r[3] for r in rows} == {"Usage", "Tax"}
    assert all(r[4] == r[5] and r[6] == "USD" for r in rows)
    projects = owner_sql(
        "SELECT provider_account_id, name, account_type FROM cloud_account "
        "WHERE connection_id = %s ORDER BY 1",
        conn,
    )
    assert ("acme-shop-prod", "Acme Shop Prod", "member") in projects
    tags = owner_sql(
        "SELECT t.tags FROM cost_daily d JOIN tag_set t ON t.hash = d.tag_set_hash "
        "WHERE d.connection_id = %s LIMIT 1",
        conn,
    )
    assert tags and tags[0][0]["team"] == "core"
    batches = owner_sql(
        "SELECT mapping_version, manifest_key, status FROM ingest_batch WHERE connection_id = %s",
        conn,
    )
    assert {b[0] for b in batches} == {"gcp-focus-1"} and {b[2] for b in batches} == {"succeeded"}
    assert batches[0][1] == f"bq://{EXPORT_PROJECT}.{DATASET}.{TABLE}"
    run = owner_sql(
        "SELECT scope->>'bytesBilled', est_customer_api_cost_usd FROM sync_run "
        "WHERE connection_id = %s AND job_type = 'cur.ingest'",
        conn,
    )
    assert int(run[0][0]) > 0

    # Nothing changed: nothing written. A restated month replaces that month only.
    again = await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
    assert again.status == "up_to_date" and again.loaded == []
    this = max(gcp.rows)
    gcp.rows[this] = gcp.rows[this][:-1]  # the tax line is gone
    restated = await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
    assert restated.loaded_periods == [this]
    statuses = owner_sql(
        "SELECT status, count(*) FROM ingest_batch WHERE connection_id = %s AND "
        "billing_period = %s GROUP BY 1 ORDER BY 1",
        conn,
        this,
    )
    assert statuses == [("succeeded", 1), ("superseded", 1)]

    # The 6-hourly poll picks Google Cloud connections up (they have no CUR bucket).
    assert (tenant, conn) in await ingestion_jobs._connections(with_export=True)

    # Reconciliation: equal right after the load, a mismatch once BigQuery changes.
    assert await billing.reconcile(uuid.UUID(tenant), uuid.UUID(conn), this) == "ok"
    gcp.rows[this] = [*gcp.rows[this], *focus_rows(this, projects=(PROJECTS[0],))[:5]]
    assert await billing.reconcile(uuid.UUID(tenant), uuid.UUID(conn), this) == "mismatch"


async def test_bills_in_another_currency_convert_to_usd(
    client: AsyncClient, login, owner_sql: Sql
) -> None:
    fake = FakeGcp().bill(currency="NGN", rate=1500.0).grant_all()
    install(fake)
    try:
        tenant, _, conn = await connected(client, login, fake)
        result = await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
        assert result.status == "succeeded", result.errors
        rows = owner_sql(
            "SELECT billing_currency, sum(billed_cost), sum(billed_cost_usd) FROM cost_daily "
            "WHERE connection_id = %s GROUP BY 1",
            conn,
        )
        assert rows[0][0] == "NGN"
        assert float(rows[0][2]) == pytest.approx(float(rows[0][1]) / 1500, rel=1e-4)
        checks = owner_sql(
            "SELECT checks FROM ingest_batch WHERE connection_id = %s LIMIT 1", conn
        )[0][0]
        assert {c["id"]: c["status"] for c in checks}["fx"] == "warning"  # Google's own rate

        # Neither a staff rate nor Google's: the period is blocked, nothing is replaced.
        for period in fake.rows:
            for r in fake.rows[period]:
                r["x_CurrencyConversionRate"] = None
            fake.rows[period].append(dict(fake.rows[period][0]))
        blocked = await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
        assert blocked.status == "failed" and blocked.rejected
        failed = owner_sql(
            "SELECT checks FROM ingest_batch WHERE connection_id = %s AND status = 'failed' "
            "LIMIT 1",
            conn,
        )[0][0]
        assert {c["id"]: c["status"] for c in failed}["fx"] == "error"
    finally:
        uninstall()


async def test_reload_reconcile_and_discovery_through_the_shared_routes(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, headers, conn = await connected(client, login, gcp)
    r = await client.post(f"/v1/tenants/{tenant}/connections/{conn}/reload", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert {p["outcome"] for p in body["periods"]} == {"reloaded"}
    recon = owner_sql("SELECT status FROM cost_reconciliation WHERE connection_id = %s", conn)
    assert recon and {s for (s,) in recon} == {"ok"}

    owner_sql("DELETE FROM sync_run WHERE connection_id = %s AND job_type = 'cur.reload'", conn)
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/{conn}/reload",
        json={"period": f"{max(gcp.rows):%Y-%m}"},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["periods"][0]["outcome"] == "unchanged"

    assert await billing.discover(uuid.UUID(tenant), uuid.UUID(conn)) == "succeeded"
    listed = owner_sql(
        "SELECT provider_account_id, org_status FROM cloud_account WHERE connection_id = %s "
        "AND account_type = 'member' ORDER BY 1",
        conn,
    )
    assert listed == [(p, "ACTIVE") for p in sorted(PROJECTS)]
    accounts = await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)
    assert {a["provider"] for a in accounts.json()["accounts"]} == {"gcp"}


# --- resources, Recommender, metrics and rules -------------------------------------------


async def test_scan_metrics_and_recommendations(
    client: AsyncClient, login, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, headers, conn = await connected(client, login, gcp)
    await billing.ingest(uuid.UUID(tenant), uuid.UUID(conn))
    r = await client.post(f"/v1/tenants/{tenant}/connections/{conn}/scan", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    assert {a["awsAccountId"]: a["access"] for a in r.json()["accounts"]} == {
        p: True for p in PROJECTS
    }
    types = owner_sql(
        "SELECT resource_type, count(*) FROM resource WHERE connection_id = %s AND "
        "provider = 'gcp' AND deleted_at IS NULL GROUP BY 1 ORDER BY 1",
        conn,
    )
    assert dict(types) == {
        "cloudsql_instance": 1, "gce_address": 2, "gce_disk": 2, "gce_instance": 2,
        "gce_snapshot": 2,
    }  # fmt: skip
    cpu = owner_sql(
        "SELECT count(*) FROM resource_metric m JOIN resource r ON r.id = m.resource_id "
        "WHERE r.connection_id = %s AND m.metric = 'cpu'",
        conn,
    )
    assert cpu[0][0] >= 14 * 3

    recs = owner_sql(
        "SELECT rule_id, provider, fingerprint, fix FROM recommendation ORDER BY rule_id"
    )
    mine = {r[0]: r for r in recs if r[1] == "gcp" and "acme-shop-prod" in r[2]}
    assert {"GCP1_idle_vm", "GCP2_unattached_disk", "GCP3_unused_address", "GCP4_old_snapshot",
            "GCP5_rightsize_vm", "GCP6_idle_cloudsql"} <= set(mine)  # fmt: skip
    disk_config = owner_sql(
        "SELECT config FROM resource WHERE connection_id = %s "
        "AND resource_id = 'disks/orphan-disk'",
        conn,
    )[0][0]
    assert disk_config["recommendations"][0]["subtype"] == "SNAPSHOT_AND_DELETE_DISK"
    assert not any("global/recommenders/google.compute.disk" in c for c in gcp.calls)
    idle = mine["GCP1_idle_vm"]
    assert idle[2] == "gcp:GCP1_idle_vm:acme-shop-prod:europe-west2-a:instances/web-idle-1"
    assert idle[3]["steps"][0].startswith("gcloud compute instances stop web-idle-1")
    assert idle[3]["rollback"][0].startswith("gcloud compute instances start")
    assert idle[3]["console"]["url"].startswith("https://console.cloud.google.com/")
    assert "n2-standard-4" in mine["GCP5_rightsize_vm"][3]["steps"][1]

    # A resource gone from Google is gone here; its recommendation is checked by verification.
    gcp.resources[PROJECTS[0]]["addresses"] = []
    assert await inventory.scan(uuid.UUID(tenant), uuid.UUID(conn)) == "succeeded"
    gone = owner_sql(
        "SELECT deleted_at IS NOT NULL FROM resource WHERE resource_id = 'addresses/old-ip' AND "
        "connection_id = %s",
        conn,
    )
    assert gone == [(True,)]

    # A project without the roles: costs still load, the scan says which project.
    gcp.permissions[PROJECTS[1]] = set()
    assert await inventory.scan(uuid.UUID(tenant), uuid.UUID(conn)) == "partial"
    body = await validate(client, tenant, headers, conn)
    projects = next(c for c in body["permissionChecks"] if c["id"] == "projects")
    assert projects["status"] == "warning" and PROJECTS[1] in projects["hint"]

    # Disconnect expires the open Google Cloud recommendations.
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{conn}", headers=headers)
    assert r.status_code == 204
    open_ = owner_sql(
        "SELECT count(*) FROM recommendation WHERE provider = 'gcp' AND status = 'new' AND "
        "tenant_id = %s",
        tenant,
    )
    assert open_ == [(0,)]


def _resource(rtype: str, rid: str, state: str, config: dict[str, Any], **kw: Any) -> Resource:
    return Resource(
        id=uuid.uuid4(),
        account_id="acme-shop-prod",
        region_id=kw.pop("region", "europe-west2-a"),
        resource_type=rtype,
        resource_id=rid,
        name=rid,
        state=state,
        config=config,
        tags=kw.pop("tags", {}),
        state_since=kw.pop("since", NOW - timedelta(days=40)),
        first_seen_at=NOW - timedelta(days=60),
        provider="gcp",
    )


def snap(name: str, **config: Any) -> Resource:
    return _resource(
        "gce_snapshot", name, "READY", {"storageBytes": 10**11, **config}, region="global"
    )


def test_rules_skip_what_they_should() -> None:
    old = (NOW - timedelta(days=200)).isoformat()
    ldn, day_ago = "europe-west2", NOW - timedelta(days=1)
    resources = [
        # Busy: no CPU data at all, so not idle.
        _resource("gce_instance", "busy", "RUNNING", {"machineType": "e2-standard-2"}),
        _resource("gce_disk", "used", "in_use", {"type": "pd-ssd", "sizeGb": 10}),
        _resource("gce_address", "internal", "RESERVED", {"addressType": "INTERNAL"}, region=ldn),
        _resource("gce_address", "fresh", "RESERVED", {}, region=ldn, since=day_ago),
        snap("scheduled", creationTimestamp=old, autoCreated=True),
        snap("live-source", creationTimestamp=old, sourceDisk="used"),
        snap("young", creationTimestamp=NOW.isoformat()),
        _resource("gce_disk", "regional", "unattached", {"type": "pd-balanced", "sizeGb": 100,
                  "regional": True}, region=ldn),
    ]  # fmt: skip
    ctx = Context(now=NOW, prices=PriceBook(list_prices={}))
    found = {
        (c.rule_id, c.resource.resource_id): c
        for spec in GCP_RULES
        if spec.evaluate is not None
        for c in spec.evaluate(ctx, resources)
    }
    assert set(found) == {
        ("GCP3_unused_address", "fresh"),
        ("GCP2_unattached_disk", "regional"),
    }
    assert found[("GCP3_unused_address", "fresh")].confidence == "low"
    regional = found[("GCP2_unattached_disk", "regional")]
    assert regional.cost_now == pytest.approx(20.0)  # 100 GB x $0.10 x 2 zones
    assert "--region europe-west2" in regional.fix["steps"][1]
    scored = engine.evaluate(ctx, resources)
    assert {s.fingerprint.split(":")[0] for s in scored} == {"gcp"}


async def test_client_pages_and_caps_rows(gcp: FakeGcp) -> None:
    sa = gcp.add_account()
    gcp.customer_setup(sa)
    api = client.GcpApi(service_account=sa)
    period = max(gcp.rows)
    gs = settings_row(EXPORT_PROJECT, sa)
    sql = billing.daily_sql(gs, set(FOCUS_FIELDS))
    result = await api.query(EXPORT_PROJECT, sql, [client.param("period", "STRING", period)])
    assert len(result.rows) > 40  # more than one fake page
    assert sum(1 for c in gcp.calls if c.startswith("GET bigquery")) >= 1
    with pytest.raises(client.GcpError, match="TooManyRows"):
        await api.query(
            EXPORT_PROJECT, sql, [client.param("period", "STRING", period)], max_rows=10
        )
    api.settings.max_bytes_billed = 1
    try:
        with pytest.raises(client.GcpError) as err:
            await api.query(EXPORT_PROJECT, sql, [])
        assert err.value.code == "bytesBilledLimitExceeded"
    finally:
        api.settings.max_bytes_billed = 20 * 1024**3
    sql_missing = billing.daily_sql(gs, {"BilledCost"})
    assert "CAST(NULL AS STRING) AS tags" in sql_missing and "NULL AS list_cost" in sql_missing
    assert "TO_JSON_STRING(x_Labels) AS tags" in sql  # the FOCUS export has no Tags column
    assert "TO_JSON_STRING(Tags) AS tags" in billing.daily_sql(gs, {"Tags"})
    assert "INTERVAL 1 DAY" in sql and "BillingPeriodStart = " not in sql

    # A query still running: the client waits on getQueryResults; bytes billed come from
    # jobs.query (GetQueryResultsResponse has none).
    gcp.slow_jobs = True
    slow = await api.query(EXPORT_PROJECT, sql, [client.param("period", "STRING", period)])
    assert slow.rows == result.rows and slow.bytes_billed == gcp.bytes_per_query


async def test_transient_errors_are_retried_and_quota_is_not_denial(gcp: FakeGcp) -> None:
    sa = gcp.add_account()
    gcp.customer_setup(sa)
    api = client.GcpApi(service_account=sa)
    api.settings.retry_base_seconds = 0
    try:
        gcp.flaky["bigquery.googleapis.com/bigquery/v2/projects/acme-billing/datasets"] = [
            (503, "backendError"),
            (403, "rateLimitExceeded"),
            (429, "RATE_LIMIT_EXCEEDED"),
        ]
        dataset = await api.dataset(EXPORT_PROJECT, DATASET)
        assert dataset["datasetReference"]["datasetId"] == DATASET
        gcp.flaky["compute.googleapis.com"] = [(403, "rateLimitExceeded")] * 9
        with pytest.raises(client.GcpError) as err:
            await api.request("GET", f"{api.settings.compute_url}/projects/p/global/snapshots")
        assert err.value.code == "rateLimitExceeded" and not err.value.denied
        assert err.value.retryable
        gcp.flaky.clear()
        gcp.failures["bigquery.googleapis.com/bigquery/v2/projects/nope"] = (403, "accessDenied")
        with pytest.raises(client.GcpError) as err:
            await api.dataset("nope", DATASET)
        assert err.value.denied and not err.value.retryable
    finally:
        api.settings.retry_base_seconds = 1.0


def test_recommendations_find_their_target() -> None:
    from fake_gcp import default_recommendations

    recs = default_recommendations()
    disk = next(v for k, v in recs.items() if k[2].endswith("disk.IdleResourceRecommender"))[0]
    # The idle-disk recommendation adds a snapshot before removing the disk.
    assert inventory.target_names(disk)[0] == "orphan-disk"
    summary = inventory.recommendation_summary(disk, "google.compute.disk.IdleResourceRecommender")
    assert summary["savingsMonthly"] == pytest.approx(85 * 30.4167 / 30, rel=1e-3)
    assert inventory._seconds({"seconds": 2592000}) == 2592000


async def test_workspace_deletion_says_what_to_remove_in_google_cloud(
    client: AsyncClient, login, gcp, owner_sql
) -> None:
    """Deleting the workspace deletes each connection's own Cloudoptics account, so the
    customer has nothing to undo; the page and email say so (and nothing about AWS stacks)."""
    from test_data_deletion import Outbox

    from cloudoptics.notifications.email import set_mailer

    tenant, headers, conn = await connected(client, login, gcp)
    sa = owner_sql("SELECT role_arn FROM cloud_connection WHERE id = %s", conn)[0][0]
    url = f"/v1/tenants/{tenant}/deletion"
    view = (await client.get(url, headers=headers)).json()
    assert view["stacks"] == []
    [removal] = view["removals"]
    assert removal["provider"] == "gcp"
    assert removal["label"] == f"Google Cloud billing account {gcp.billing_account.upper()}"
    assert removal["commands"] == []
    assert sa in removal["steps"][0] and "stops working at once" in removal["steps"][0]
    box = Outbox()
    set_mailer(box)
    try:
        r = await client.post(url, json={"confirmName": "Acme Cloud Ltd"}, headers=headers)
        assert r.status_code == 202, r.text
    finally:
        set_mailer(None)
    [mail] = box.sent
    assert f"Google Cloud billing account {gcp.billing_account.upper()}:" in mail.text
    assert "Nothing else to do" in mail.text
    assert "CloudFormation" not in mail.text and "AWS" not in mail.text
    from cloudoptics.privacy import jobs

    deletion = r.json()["deletion"]["id"]
    owner_sql(
        "UPDATE data_deletion SET scheduled_for = now() - interval '1 minute' WHERE id = %s",
        deletion,
    )
    done = await jobs.execute(uuid.UUID(tenant), uuid.UUID(deletion), datetime.now(UTC))
    assert done == "completed"
    assert sa not in gcp.service_accounts  # deleted with the workspace
