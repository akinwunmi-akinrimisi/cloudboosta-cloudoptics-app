"""Ingestion end to end against PostgreSQL: export folder -> cost_daily -> costs summary."""

import json
import secrets
from pathlib import Path

import pytest
from httpx import AsyncClient

from cloudoptics.ingestion import jobs, service
from cloudoptics.ingestion.source import LocalExportSource
from cur_fixture import BillingPeriod, generate, write_export

SEPT = BillingPeriod(2026, 9)
FIXTURE_TOTAL = round(sum(generate(SEPT).table["line_item_unblended_cost"].to_pylist()), 2)


@pytest.fixture
def bucket(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "bucket"
    root.mkdir()
    service.set_source_factory(lambda conn, tenant_id: LocalExportSource(root))
    yield root
    service.set_source_factory(None)


async def connected(client: AsyncClient, login, owner_sql) -> tuple[str, str, dict[str, str]]:  # type: ignore[no-untyped-def]
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": "W", "countryCode": "NG"}, headers=headers)
    tenant = r.json()["id"]
    account = "".join(secrets.choice("0123456789") for _ in range(12))
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": account}, headers=headers
    )
    connection = r.json()["connection"]["id"]
    owner_sql(
        "UPDATE cloud_connection SET status = 'healthy', cur_bucket = 'b', cur_prefix = "
        "'cloudoptics' WHERE id = %s",
        connection,
    )
    return tenant, connection, headers


async def summary(client: AsyncClient, tenant: str, headers: dict[str, str], today: str) -> dict:  # type: ignore[type-arg]
    r = await client.get(
        f"/v1/tenants/{tenant}/costs/summary", params={"today": today}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def ingest(
    client: AsyncClient, tenant: str, connection: str, headers: dict[str, str]
) -> dict:  # type: ignore[type-arg]
    r = await client.post(f"/v1/tenants/{tenant}/connections/{connection}/ingest", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_summary_waits_for_the_first_report(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    headers = await login()
    r = await client.post("/v1/tenants", json={"name": "W", "countryCode": "NG"}, headers=headers)
    empty = await summary(client, r.json()["id"], headers, "2026-09-30")
    assert empty["status"] == "no_connection"

    tenant, connection, headers = await connected(client, login, owner_sql)
    assert (await ingest(client, tenant, connection, headers))["status"] == "up_to_date"
    body = await summary(client, tenant, headers, "2026-09-30")
    assert body["status"] == "waiting"
    assert body["monthToDate"] is None
    assert body["forecast"] is None


async def test_ingest_fills_the_summary_and_is_idempotent(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")

    first = await ingest(client, tenant, connection, headers)
    assert first == {
        "connectionId": connection,
        "status": "succeeded",
        "loaded": ["2026-09@2026-09-v1"],
        "rejected": [],
    }
    again = await ingest(client, tenant, connection, headers)
    assert again["status"] == "up_to_date"

    body = await summary(client, tenant, headers, "2026-09-15")
    assert body["status"] == "ready"
    assert body["month"] == "2026-09"
    assert body["dataThrough"] == "2026-09-30"
    assert body["lastLoadStatus"] == "succeeded"
    mtd = body["monthToDate"]
    assert mtd["billed"] == pytest.approx(FIXTURE_TOTAL, abs=0.02)
    assert mtd["effective"] == pytest.approx(FIXTURE_TOTAL, abs=0.02)
    assert mtd["credits"] == pytest.approx(-60.0)
    assert body["forecast"]["daysCovered"] == 30
    assert body["forecast"]["amount"] == pytest.approx(mtd["effective"], abs=0.01)

    october = await summary(client, tenant, headers, "2026-10-02")
    assert october["monthToDate"]["effective"] == 0
    assert october["forecast"] is None
    assert october["previousMonth"] == pytest.approx(FIXTURE_TOTAL, abs=0.02)

    rows = owner_sql(
        "SELECT count(*), count(DISTINCT data_version) FROM cost_daily WHERE connection_id = %s",
        connection,
    )
    count, versions = rows[0]
    assert count > 0
    assert versions == 1
    names = owner_sql(
        "SELECT aws_account_id, name, account_type FROM cloud_account WHERE tenant_id = %s "
        "ORDER BY aws_account_id",
        tenant,
    )
    assert ("333333333333", "demo-dev", "member") in names
    files = owner_sql(
        "SELECT status, length(sha256), row_count FROM billing_file WHERE connection_id = %s",
        connection,
    )
    assert files == [("aggregated", 64, generate(SEPT).table.num_rows)]
    runs = owner_sql(
        "SELECT status, records FROM sync_run WHERE connection_id = %s AND job_type = "
        "'cur.ingest' ORDER BY started_at",
        connection,
    )
    assert runs == [("succeeded", files[0][2]), ("succeeded", 0)]


async def test_restatement_replaces_the_period_atomically(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    before = (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"]["effective"]

    write_export(generate(SEPT, restate=True), bucket / "cloudoptics")
    result = await ingest(client, tenant, connection, headers)
    assert result["loaded"] == ["2026-09@2026-09-v2"]
    after = (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"]["effective"]
    assert after == pytest.approx(before - 25.0, abs=0.01)  # the refund

    batches = owner_sql(
        "SELECT export_version, status FROM ingest_batch WHERE connection_id = %s "
        "ORDER BY export_version",
        connection,
    )
    assert batches == [("2026-09-v1", "superseded"), ("2026-09-v2", "succeeded")]
    versions = owner_sql(
        "SELECT DISTINCT data_version FROM cost_daily WHERE connection_id = %s", connection
    )
    assert versions == [("2026-09-v2",)]


async def test_a_version_that_fails_checks_keeps_the_previous_data(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    manifest_path = write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    good = (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"]

    manifest = json.loads(manifest_path.read_text())
    manifest["executionId"] = "2026-09-v9"
    manifest["numRows"] += 1  # a delivery that is missing rows
    manifest_path.write_text(json.dumps(manifest))
    result = await ingest(client, tenant, connection, headers)
    assert result["status"] == "failed"
    assert result["rejected"] == ["2026-09@2026-09-v9"]

    body = await summary(client, tenant, headers, "2026-09-30")
    assert body["monthToDate"] == good
    assert body["lastLoadStatus"] == "failed"
    checks = owner_sql(
        "SELECT checks FROM ingest_batch WHERE export_version = '2026-09-v9' AND "
        "connection_id = %s",
        connection,
    )[0][0]
    assert {"id": "completeness", "status": "error"}.items() <= next(
        c for c in checks if c["id"] == "completeness"
    ).items()
    # The rejected version is not retried on the next poll.
    assert (await ingest(client, tenant, connection, headers))["status"] == "up_to_date"


async def test_unreadable_bucket_is_recorded_as_a_failed_run(
    client: AsyncClient, login, owner_sql
) -> None:
    from fake_aws import client_error

    def denied(conn, tenant_id):  # type: ignore[no-untyped-def]
        raise client_error("AccessDenied", "ListObjectsV2")

    service.set_source_factory(denied)
    try:
        tenant, connection, headers = await connected(client, login, owner_sql)
        assert (await ingest(client, tenant, connection, headers))["status"] == "failed"
    finally:
        service.set_source_factory(None)
    runs = owner_sql(
        "SELECT status, error_code, error_detail FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'cur.ingest'",
        connection,
    )
    assert runs == [("failed", "load_failed", "AccessDenied")]


async def test_disconnected_or_exportless_connections_are_skipped(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET status = 'disconnected' WHERE id = %s", connection)
    assert (await ingest(client, tenant, connection, headers))["status"] == "skipped"


async def test_costs_are_isolated_per_workspace(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)

    other, _, other_headers = await connected(client, login, owner_sql)
    assert (await summary(client, other, other_headers, "2026-09-30"))["status"] == "waiting"
    r = await client.get(
        f"/v1/tenants/{tenant}/costs/summary", params={"today": "2026-09-30"}, headers=other_headers
    )
    assert r.status_code == 404
    r = await client.post(
        f"/v1/tenants/{other}/connections/{connection}/ingest", headers=other_headers
    )
    assert r.status_code == 404


async def test_viewers_cannot_trigger_an_ingest(
    client: AsyncClient, login, owner_sql, bucket
) -> None:
    tenant, connection, _ = await connected(client, login, owner_sql)
    viewer = await login()
    me = (await client.get("/v1/me", headers=viewer)).json()
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, 'viewer')",
        tenant,
        me["id"],
    )
    r = await client.post(f"/v1/tenants/{tenant}/connections/{connection}/ingest", headers=viewer)
    assert r.status_code == 403
    r = await client.get(f"/v1/tenants/{tenant}/costs/summary", headers=viewer)
    assert r.status_code == 200


async def test_poll_queues_every_connection_with_an_export(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    _, healthy, _ = await connected(client, login, owner_sql)
    _, other_tenant_conn, _ = await connected(client, login, owner_sql)
    _, gone, _ = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET status = 'disconnected' WHERE id = %s", gone)

    queued: list[tuple[str, str]] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append((tenant_id, connection_id))

    import cloudoptics.platform.jobs as platform_jobs

    monkeypatch.setattr(jobs, "defer_ingest", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    count = await jobs.poll(0)
    ids = {c for _, c in queued}
    assert {healthy, other_tenant_conn} <= ids
    assert gone not in ids
    assert count == len(queued)


async def test_queue_mode_defers_the_ingest(client: AsyncClient, login, owner_sql) -> None:
    from cloudoptics.platform.jobs import get_job_app
    from cloudoptics.platform.settings import get_settings

    settings = get_settings()
    get_job_app.cache_clear()
    tenant, connection, headers = await connected(client, login, owner_sql)
    settings.jobs_mode = "queue"
    try:
        for _ in range(2):  # the second request finds the job already queued
            r = await client.post(
                f"/v1/tenants/{tenant}/connections/{connection}/ingest", headers=headers
            )
            assert r.status_code == 202
            assert r.json()["status"] == "queued"
    finally:
        settings.jobs_mode = "inline"
    queued = owner_sql(
        "SELECT task_name FROM procrastinate_jobs WHERE status = 'todo'"
        " AND args->>'connection_id' = %s",
        connection,
    )
    assert queued == [("ingestion:cur.ingest",)]
