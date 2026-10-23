"""Usage metrics: candidates, 32-day backfill then incremental pulls, budget and retention."""

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from test_inventory import setup, world  # noqa: F401 - fixture

from cloudoptics.inventory import scanner
from cloudoptics.metrics import puller
from cloudoptics.platform.settings import get_settings

NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)


async def scanned(client: AsyncClient, login, owner_sql, world) -> tuple[uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]  # noqa: F811
    tenant, connection, _, _ = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid, cid = uuid.UUID(tenant), uuid.UUID(connection)
    assert await scanner.scan_connection(tid, cid) == "succeeded"
    return tid, cid


def stored(owner_sql, tenant: uuid.UUID) -> dict[tuple[str, str, str], int]:  # type: ignore[no-untyped-def]
    rows = owner_sql(
        "SELECT r.region_id, r.resource_id, m.metric, count(*) FROM resource_metric m "
        "JOIN resource r ON r.id = m.resource_id WHERE m.tenant_id = %s "
        "GROUP BY 1, 2, 3",
        tenant,
    )
    return {(r, i, m): n for r, i, m, n in rows}


async def test_first_pull_backfills_32_days_for_running_servers_and_databases(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, connection = await scanned(client, login, owner_sql, world)
    # One server is running; the other stays stopped and has no usage to pull.
    owner_sql("UPDATE resource SET state = 'running' WHERE resource_id = 'i-eu-west-1-1'")
    world.metric_values = {"CPUUtilization": 3.5, "DatabaseConnections": 0.0}

    assert await puller.pull_connection(tenant, connection, now=NOW) == "succeeded"

    starts = {c["StartTime"] for c in world.metric_calls}
    assert starts == {datetime(2026, 9, 18, tzinfo=UTC)}
    assert {c["EndTime"] for c in world.metric_calls} == {datetime(2026, 10, 20, 12, tzinfo=UTC)}
    # 33 days: 18 September to 20 October, the last one partial.
    assert stored(owner_sql, tenant) == {
        ("eu-west-1", "i-eu-west-1-1", "cpu"): 33,
        ("eu-west-1", "i-eu-west-1-1", "cpu_max"): 33,
        ("eu-west-1", "i-eu-west-1-1", "network_in"): 33,
        ("eu-west-1", "i-eu-west-1-1", "network_out"): 33,
        ("eu-west-1", "orders-db", "cpu"): 33,
        ("eu-west-1", "orders-db", "cpu_max"): 33,
        ("eu-west-1", "orders-db", "connections"): 33,
        ("eu-west-1", "orders-db", "read_iops"): 33,
        ("eu-west-1", "orders-db", "write_iops"): 33,
        ("af-south-1", "orders-db", "cpu"): 33,
        ("af-south-1", "orders-db", "cpu_max"): 33,
        ("af-south-1", "orders-db", "connections"): 33,
        ("af-south-1", "orders-db", "read_iops"): 33,
        ("af-south-1", "orders-db", "write_iops"): 33,
    }
    last = owner_sql(
        "SELECT hourly FROM resource_metric m JOIN resource r ON r.id = m.resource_id "
        "WHERE r.resource_id = 'i-eu-west-1-1' AND m.metric = 'cpu' AND m.day = '2026-10-20'"
    )[0][0]
    assert last[:12] == [3.5] * 12 and last[12:] == [None] * 12

    run = owner_sql(
        "SELECT status, records, scope FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'metrics.pull'",
        connection,
    )
    assert run == [("succeeded", 462, {"resources": 3, "queries": 14, "over_budget": 0})]
    assert owner_sql(
        "SELECT units FROM aws_api_usage WHERE tenant_id = %s AND day = '2026-10-20'", tenant
    ) == [(14,)]

    resource = owner_sql(
        "SELECT id FROM resource WHERE resource_id = 'i-eu-west-1-1' AND tenant_id = %s", tenant
    )[0][0]
    from cloudoptics.platform.db import get_sessionmaker, scope

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant)
        series = {s.metric: s for s in await puller.usage(session, resource)}
    assert series["cpu"].days[0] == (date(2026, 9, 18), pytest.approx(3.5), pytest.approx(3.5))
    assert len(series["cpu"].days) == 33


async def test_later_pulls_start_from_the_last_stored_day_and_old_days_are_removed(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, connection = await scanned(client, login, owner_sql, world)
    await puller.pull_connection(tenant, connection, now=NOW)
    db = owner_sql(
        "SELECT id FROM resource WHERE resource_id = 'orders-db' AND region_id = 'eu-west-1' "
        "AND tenant_id = %s",
        tenant,
    )[0][0]
    owner_sql(
        "INSERT INTO resource_metric (tenant_id, resource_id, metric, day, hourly) "
        "VALUES (%s, %s, 'cpu', '2026-09-01', array_fill(1::real, ARRAY[24]))",
        tenant,
        db,
    )
    world.metric_calls.clear()

    later = datetime(2026, 10, 21, 6, tzinfo=UTC)
    assert await puller.pull_connection(tenant, connection, now=later) == "succeeded"
    assert {c["StartTime"] for c in world.metric_calls} == {datetime(2026, 10, 20, tzinfo=UTC)}
    days = owner_sql(
        "SELECT min(day), max(day), count(*) FROM resource_metric WHERE resource_id = %s "
        "AND metric = 'cpu'",
        db,
    )
    # 1 September is past the 40 days kept; 20 October is now complete.
    assert days == [(date(2026, 9, 18), date(2026, 10, 21), 34)]
    full = owner_sql(
        "SELECT hourly FROM resource_metric WHERE resource_id = %s AND metric = 'cpu' "
        "AND day = '2026-10-20'",
        db,
    )[0][0]
    assert None not in full


async def test_daily_budget_limits_queries_and_new_resources_go_first(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
    monkeypatch,
) -> None:
    tenant, connection = await scanned(client, login, owner_sql, world)
    monkeypatch.setattr(get_settings(), "metrics_daily_budget", 5)

    assert await puller.pull_connection(tenant, connection, now=NOW) == "partial"
    assert len({k[:2] for k in stored(owner_sql, tenant)}) == 1  # one database of two
    run = owner_sql(
        "SELECT status, error_detail FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'metrics.pull'",
        connection,
    )[0]
    assert run == ("partial", "budget: 1 resources wait for tomorrow's budget")

    # Same day: the budget is spent, so nothing is called.
    world.metric_calls.clear()
    assert await puller.pull_connection(tenant, connection, now=NOW) == "partial"
    assert world.metric_calls == []

    # Next day: the database without data is pulled before the one that has it.
    monkeypatch.setattr(get_settings(), "metrics_daily_budget", 5)
    next_day = datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert await puller.pull_connection(tenant, connection, now=next_day) == "partial"
    assert len({k[:2] for k in stored(owner_sql, tenant)}) == 2


async def test_region_errors_are_partial_and_no_access_or_unusable_is_skipped(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, connection = await scanned(client, login, owner_sql, world)
    world.broken_regions = {"af-south-1"}
    assert await puller.pull_connection(tenant, connection, now=NOW) == "partial"
    detail = owner_sql(
        "SELECT error_detail FROM sync_run WHERE connection_id = %s AND job_type = 'metrics.pull'",
        connection,
    )[0][0]
    assert "af-south-1: Throttling" in detail
    assert {k[0] for k in stored(owner_sql, tenant)} == {"eu-west-1"}

    world.broken_regions = {"af-south-1", "eu-west-1"}
    assert await puller.pull_connection(tenant, connection, now=NOW) == "failed"

    owner_sql("UPDATE cloud_account SET resource_access = false WHERE tenant_id = %s", tenant)
    assert await puller.pull_connection(tenant, connection, now=NOW) == "skipped"

    owner_sql("UPDATE cloud_connection SET status = 'failed' WHERE id = %s", connection)
    assert await puller.pull_connection(tenant, connection, now=NOW) == "skipped"


async def test_pulled_usage_feeds_the_idle_rules(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    from cloudoptics.analysis import engine

    tenant, connection = await scanned(client, login, owner_sql, world)
    owner_sql("UPDATE resource SET state = 'running' WHERE resource_id = 'i-eu-west-1-1'")
    world.metric_values = {"CPUUtilization": 1.0, "DatabaseConnections": 0.0}
    now = datetime.now(UTC)
    await puller.pull_connection(tenant, connection, now=now)
    await engine.run_analysis(tenant, now=now)

    rows = owner_sql(
        "SELECT rule_id, region_id, resource_id FROM recommendation WHERE tenant_id = %s "
        "AND rule_id LIKE 'R4%%' ORDER BY 1, 2",
        tenant,
    )
    assert rows == [
        ("R4_idle_ec2", "eu-west-1", "i-eu-west-1-1"),
        ("R4_idle_rds", "af-south-1", "orders-db"),
        ("R4_idle_rds", "eu-west-1", "orders-db"),
    ]
