"""AWS Organizations account discovery and account coverage."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from fake_aws import FakeClient, FakeSessionFactory, client_error, healthy_clients
from httpx import AsyncClient
from test_ingestion import SEPT, connected, ingest

from cloudoptics.connectors import accounts
from cloudoptics.connectors import service as connectors
from cloudoptics.ingestion import service
from cloudoptics.ingestion.source import LocalExportSource
from cur_fixture import generate, write_export


class FakeOrganizations:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages

    def list_accounts(self, **request: Any) -> dict[str, Any]:
        index = int(request.get("NextToken", 0))
        page: dict[str, Any] = {"Accounts": self.pages[index]}
        if index + 1 < len(self.pages):
            page["NextToken"] = str(index + 1)
        return page


def org_accounts(payer: str) -> list[list[dict[str, Any]]]:
    return [
        [
            {"Id": payer, "Name": "payer", "Status": "ACTIVE"},
            {"Id": "333333333333", "Name": "demo-dev", "State": "ACTIVE"},
        ],
        [{"Id": "444444444444", "Name": "sandbox", "Status": "SUSPENDED"}],
    ]


@pytest.fixture
def org():  # type: ignore[no-untyped-def]
    clients = healthy_clients()
    factory = FakeSessionFactory(clients)
    connectors.set_session_factory(factory)
    yield factory
    connectors.set_session_factory(None)


@pytest.fixture
def bucket(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "bucket"
    root.mkdir()
    service.set_source_factory(lambda conn, tenant_id: LocalExportSource(root))
    yield root
    service.set_source_factory(None)


async def management(client: AsyncClient, login, owner_sql) -> tuple[str, str, str, dict[str, str]]:  # type: ignore[no-untyped-def]
    tenant, connection, headers = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET account_type = 'management' WHERE id = %s", connection)
    payer = owner_sql("SELECT aws_account_id FROM cloud_connection WHERE id = %s", connection)[0][0]
    return tenant, connection, str(payer), headers


def test_list_org_accounts_pages_and_reads_state_or_status() -> None:
    result = accounts.list_org_accounts(FakeOrganizations(org_accounts("111111111111")))
    assert [(a.aws_account_id, a.status) for a in result] == [
        ("111111111111", "ACTIVE"),
        ("333333333333", "ACTIVE"),
        ("444444444444", "SUSPENDED"),
    ]


async def test_discovery_lists_accounts_and_coverage(
    client: AsyncClient, login, owner_sql, org, bucket
) -> None:
    tenant, connection, payer, headers = await management(client, login, owner_sql)
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)
    org.clients["organizations"] = FakeOrganizations(org_accounts(payer))  # type: ignore[assignment]

    assert await accounts.discover_accounts(tid, cid) == "succeeded"
    assert org.calls[-1][2].endswith("-orgs")
    body = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    assert [
        (a["awsAccountId"], a["name"], a["accountType"], a["orgStatus"]) for a in body["accounts"]
    ] == [
        (payer, "payer", "management", "ACTIVE"),
        ("333333333333", "demo-dev", "member", "ACTIVE"),
        ("444444444444", "sandbox", "member", "SUSPENDED"),
    ]
    assert body["coverage"] == {"accounts": 3, "withCostData": 0, "withResourceAccess": 0}

    # The sandbox leaves the organization: kept, marked, and left out of coverage.
    org.clients["organizations"] = FakeOrganizations(org_accounts(payer)[:1])  # type: ignore[assignment]
    await accounts.discover_accounts(tid, cid)
    owner_sql(
        "UPDATE cloud_account SET resource_access = true WHERE tenant_id = %s "
        "AND aws_account_id = '333333333333'",
        tenant,
    )
    body = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    assert body["accounts"][-1]["orgStatus"] == accounts.NOT_LISTED
    assert body["coverage"] == {"accounts": 2, "withCostData": 0, "withResourceAccess": 1}

    runs = owner_sql(
        "SELECT status, records FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'accounts.discover' ORDER BY started_at",
        connection,
    )
    assert runs == [("succeeded", 3), ("succeeded", 2)]


async def test_discovery_skips_standalone_and_records_failures(
    client: AsyncClient, login, owner_sql, org
) -> None:
    tenant, connection, _ = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET account_type = 'standalone' WHERE id = %s", connection)
    assert await accounts.discover_accounts(uuid.UUID(tenant), uuid.UUID(connection)) == "skipped"

    tenant, connection, _, _ = await management(client, login, owner_sql)
    org.clients["organizations"] = FakeClient(list_accounts=client_error("AccessDeniedException"))
    status = await accounts.discover_accounts(uuid.UUID(tenant), uuid.UUID(connection))
    assert status == "failed"
    assert owner_sql(
        "SELECT status, error_code FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'accounts.discover'",
        connection,
    ) == [("failed", "AccessDeniedException")]


async def test_cost_data_counts_towards_coverage(
    client: AsyncClient, login, owner_sql, org, bucket, monkeypatch
) -> None:
    import datetime as dt

    tenant, connection, _, headers = await management(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)

    class October(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "October":  # type: ignore[override]
            return cls(2026, 10, 5, tzinfo=tz)

    from cloudoptics.connectors import routes

    monkeypatch.setattr(routes, "datetime", October)
    body = (await client.get(f"/v1/tenants/{tenant}/accounts", headers=headers)).json()
    by_id = {a["awsAccountId"]: a for a in body["accounts"]}
    assert by_id["333333333333"]["hasCostData"] is True
    assert body["coverage"]["withCostData"] >= 2


async def test_poll_queues_discovery_for_management_connections(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.connectors import jobs

    _, payer_conn, _, _ = await management(client, login, owner_sql)
    _, standalone, _ = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET account_type = 'standalone' WHERE id = %s", standalone)
    queued: list[str] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(connection_id)

    monkeypatch.setattr(jobs, "defer_discovery", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    count = await jobs.poll_accounts(0)
    assert payer_conn in queued
    assert standalone not in queued
    assert count == len(queued)
