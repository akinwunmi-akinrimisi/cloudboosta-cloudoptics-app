# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Reload bills: read a connection's export again (admins)."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from test_ingestion import SEPT, bucket, connected, ingest, summary  # noqa: F401 - fixture
from test_reconcile import FakeCostExplorer, aws  # noqa: F401 - fixture

from cloudoptics.ingestion import jobs, reload, service
from cur_fixture import generate, write_export


async def reload_bills(
    client: AsyncClient, tenant: str, connection: str, headers: dict[str, str],
    period: str | None = None, expect: int = 200,
) -> dict[str, Any]:  # fmt: skip
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/{connection}/reload",
        json={"period": period} if period else None,
        headers=headers,
    )
    assert r.status_code == expect, r.text
    return r.json()  # type: ignore[no-any-return]


def allow_again(owner_sql, connection: str) -> None:  # type: ignore[no-untyped-def]
    """Move the connection's reloads back past the hourly limit."""
    owner_sql(
        "UPDATE sync_run SET started_at = started_at - interval '2 hours' "
        "WHERE connection_id = %s AND job_type = 'cur.reload'",
        connection,
    )


def resource_rows(owner_sql, connection: str) -> int:  # type: ignore[no-untyped-def]
    return int(
        owner_sql(  # type: ignore[call-overload]
            "SELECT count(*) FROM cost_resource_daily WHERE connection_id = %s", connection
        )[0][0]
    )


@pytest.fixture
def ce(aws):  # type: ignore[no-untyped-def]
    """Cost Explorer for the reconciliation that follows an inline reload."""
    fake = FakeCostExplorer({})
    aws.clients["ce"] = fake
    return fake


async def test_reload_fills_a_month_loaded_before_resource_costs(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
    ce,
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    before = (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"]
    rows = resource_rows(owner_sql, connection)
    assert rows > 0
    # As loaded before Sprint 5: no per-resource rows. The poll does not load it again.
    owner_sql("DELETE FROM cost_resource_daily WHERE connection_id = %s", connection)
    assert (await ingest(client, tenant, connection, headers))["status"] == "up_to_date"

    body = await reload_bills(client, tenant, connection, headers, "2026-09")
    assert body["status"] == "succeeded"
    assert body["period"] == "2026-09"
    assert body["periods"] == [
        {"period": "2026-09", "outcome": "reloaded", "version": "2026-09-v1", "detail": None}
    ]
    assert body["nextAllowedAt"] is not None
    assert resource_rows(owner_sql, connection) == rows
    assert (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"] == before

    # The history keeps the first load; the reload is a batch of its own and owns the files.
    batches = owner_sql(
        "SELECT export_version, status, reload, id FROM ingest_batch WHERE connection_id = %s "
        "ORDER BY created_at",
        connection,
    )
    assert [b[:3] for b in batches] == [
        ("2026-09-v1", "superseded", False),
        ("2026-09-v1", "succeeded", True),
    ]
    files = owner_sql(
        "SELECT status, batch_id FROM billing_file WHERE connection_id = %s", connection
    )
    assert files == [("aggregated", batches[1][3])]
    # What follows new bills ran: reconciliation (and budgets, anomalies, analysis).
    assert owner_sql(
        "SELECT billing_period FROM cost_reconciliation WHERE connection_id = %s", connection
    ) == [(date(2026, 9, 1),)]
    runs = owner_sql(
        "SELECT status, records, scope->'periods'->0->>'outcome' FROM sync_run "
        "WHERE connection_id = %s AND job_type = 'cur.reload'",
        connection,
    )
    assert runs == [("succeeded", generate(SEPT).table.num_rows, "reloaded")]
    entry = owner_sql(
        "SELECT target_type, target_id, after FROM audit_log WHERE tenant_id = %s "
        "AND action = 'ingestion.reload_requested'",
        tenant,
    )
    assert entry[0][:2] == ("cloud_connection", connection)
    assert entry[0][2]["period"] == "2026-09"
    assert entry[0][2]["periods"][0]["outcome"] == "reloaded"

    status = (
        await client.get(f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers)
    ).json()
    assert status["status"] == "succeeded"
    assert status["periods"][0]["outcome"] == "reloaded"
    assert status["nextAllowedAt"] is not None


async def test_unchanged_not_in_bucket_and_failed(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
    ce,
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    manifest_path = write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    # A month loaded long ago whose export is no longer in the bucket.
    owner_sql(
        "INSERT INTO ingest_batch (id, tenant_id, connection_id, billing_period, "
        "export_version, manifest_key, mapping_version, status) VALUES (%s, %s, %s, "
        "'2026-07-01', '2026-07-v1', 'gone', '1', 'succeeded')",
        str(uuid.uuid4()),
        tenant,
        connection,
    )

    body = await reload_bills(client, tenant, connection, headers)
    assert body["status"] == "succeeded"
    assert body["period"] is None
    assert [(p["period"], p["outcome"]) for p in body["periods"]] == [
        ("2026-07", "not_in_bucket"),
        ("2026-09", "unchanged"),
    ]
    # Nothing changed, so nothing new in the history.
    assert owner_sql(
        "SELECT count(*) FROM ingest_batch WHERE connection_id = %s AND billing_period = "
        "'2026-09-01'",
        connection,
    ) == [(1,)]

    allow_again(owner_sql, connection)
    body = await reload_bills(client, tenant, connection, headers, "2026-08")
    assert body["status"] == "failed"
    assert body["periods"] == [
        {"period": "2026-08", "outcome": "not_in_bucket", "version": None, "detail": None}
    ]
    # A failed reload does not count towards the hourly limit.
    manifest = json.loads(manifest_path.read_text())
    manifest["numRows"] += 1  # the delivery is missing rows: it fails the checks
    manifest_path.write_text(json.dumps(manifest))
    good = (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"]
    body = await reload_bills(client, tenant, connection, headers, "2026-09")
    assert body["status"] == "failed"
    assert body["periods"][0]["outcome"] == "failed"
    assert "completeness" in body["periods"][0]["detail"]
    assert (await summary(client, tenant, headers, "2026-09-30"))["monthToDate"] == good
    assert owner_sql(
        "SELECT status, reload FROM ingest_batch WHERE connection_id = %s AND "
        "billing_period = '2026-09-01' ORDER BY created_at",
        connection,
    ) == [("succeeded", False), ("failed", True)]
    assert owner_sql("SELECT status FROM billing_file WHERE connection_id = %s", connection) == [
        ("aggregated",)
    ]


async def test_a_new_version_is_loaded_as_a_first_load(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
    ce,
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await ingest(client, tenant, connection, headers)
    write_export(generate(SEPT, restate=True), bucket / "cloudoptics")
    body = await reload_bills(client, tenant, connection, headers, "2026-09")
    assert body["periods"][0]["version"] == "2026-09-v2"
    assert owner_sql(
        "SELECT export_version, status, reload FROM ingest_batch WHERE connection_id = %s "
        "ORDER BY created_at",
        connection,
    ) == [("2026-09-v1", "superseded", False), ("2026-09-v2", "succeeded", False)]
    # The poll does not load it again.
    assert (await ingest(client, tenant, connection, headers))["status"] == "up_to_date"


async def test_once_an_hour_per_connection(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
    ce,
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    await reload_bills(client, tenant, connection, headers)
    r = await client.post(f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers)
    assert r.status_code == 429
    assert r.json()["title"].startswith("Bills for this connection were reloaded less than")
    assert 0 < int(r.headers["retry-after"]) <= 3600
    allow_again(owner_sql, connection)
    assert (await reload_bills(client, tenant, connection, headers))["status"] == "succeeded"


async def test_unreadable_bucket(client: AsyncClient, login, owner_sql) -> None:
    from fake_aws import client_error

    def denied(conn, tenant_id):  # type: ignore[no-untyped-def]
        raise client_error("AccessDenied", "ListObjectsV2")

    tenant, connection, headers = await connected(client, login, owner_sql)
    service.set_source_factory(denied)
    try:
        body = await reload_bills(client, tenant, connection, headers)
    finally:
        service.set_source_factory(None)
    assert body["status"] == "failed"
    assert body["error"] == "AccessDenied"
    status = (
        await client.get(f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers)
    ).json()
    assert status["status"] == "failed"
    assert status["nextAllowedAt"] is None


async def test_who_may_reload(client: AsyncClient, login, owner_sql, bucket) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    base = f"/v1/tenants/{tenant}/connections/{connection}/reload"
    assert (await client.get(base, headers=headers)).json()["status"] == "none"
    viewer = await login()
    me = (await client.get("/v1/me", headers=viewer)).json()
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, 'member')",
        tenant,
        me["id"],
    )
    assert (await client.post(base, headers=viewer)).status_code == 403
    other, _, other_headers = await connected(client, login, owner_sql)
    r = await client.post(
        f"/v1/tenants/{other}/connections/{connection}/reload", headers=other_headers
    )
    assert r.status_code == 404
    r = await client.post(base, json={"period": "2026-9"}, headers=headers)
    assert r.status_code == 422
    owner_sql("UPDATE cloud_connection SET status = 'degraded', cur_bucket = NULL "
              "WHERE id = %s", connection)  # fmt: skip
    assert (await client.post(base, headers=headers)).status_code == 409


async def test_a_running_reload_blocks_another(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
) -> None:
    from cloudoptics.platform.db import get_sessionmaker

    tenant, connection, headers = await connected(client, login, owner_sql)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"reload:{connection}"},
        )
        r = await client.post(
            f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers
        )
    assert r.status_code == 429
    assert r.json()["title"] == "A reload of this connection is already running."


async def test_queue_mode_defers_the_reload(
    client: AsyncClient,
    login,
    owner_sql,
    bucket,
    monkeypatch,
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.analysis import jobs as analysis_jobs
    from cloudoptics.anomalies import jobs as anomaly_jobs
    from cloudoptics.budgets import jobs as budget_jobs
    from cloudoptics.platform.settings import get_settings

    tenant, connection, headers = await connected(client, login, owner_sql)
    write_export(generate(SEPT), bucket / "cloudoptics")
    deferred: list[dict[str, Any]] = []

    async def fake_defer(app, name, lock, **kwargs):  # type: ignore[no-untyped-def]
        deferred.append({"name": name, "lock": lock, **kwargs})

    for module in (jobs, budget_jobs, anomaly_jobs, analysis_jobs):
        monkeypatch.setattr(module, "defer_once", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    monkeypatch.setattr(get_settings(), "jobs_mode", "queue")

    body = await reload_bills(client, tenant, connection, headers, "2026-09", expect=202)
    assert body["status"] == "queued"
    assert deferred == [
        {
            "name": jobs.RELOAD_TASK,
            "lock": f"reload:{connection}",
            "tenant_id": tenant,
            "connection_id": connection,
            "period": "2026-09",
        }
    ]
    base = f"/v1/tenants/{tenant}/connections/{connection}/reload"
    status = (await client.get(base, headers=headers)).json()
    assert status["status"] == "running"
    assert status["period"] == "2026-09"
    assert (await client.post(base, headers=headers)).status_code == 429
    assert owner_sql(
        "SELECT after->>'status' FROM audit_log WHERE tenant_id = %s "
        "AND action = 'ingestion.reload_requested'",
        tenant,
    ) == [("queued",)]

    # The worker takes the queued run over, then queues what follows new bills.
    deferred.clear()
    assert await jobs.reload(tenant, connection, "2026-09") == "succeeded"
    assert [d["name"] for d in deferred] == [
        jobs.RECONCILE_TASK,
        budget_jobs.EVALUATE_TASK,
        anomaly_jobs.DETECT_TASK,
        analysis_jobs.RUN_TASK,
    ]
    runs = owner_sql(
        "SELECT status FROM sync_run WHERE connection_id = %s AND job_type = 'cur.reload'",
        connection,
    )
    assert runs == [("succeeded",)]
    status = (await client.get(base, headers=headers)).json()
    assert status["status"] == "succeeded"
    assert status["periods"][0]["outcome"] in ("reloaded", "unchanged")

    # Nothing reloaded: nothing queued after it.
    deferred.clear()
    assert await jobs.reload(tenant, connection, "") == "succeeded"
    assert deferred == []


async def test_a_reload_left_running_is_shown_as_failed(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    owner_sql(
        "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
        "started_at) VALUES (%s, %s, %s, 'cur.reload', '{\"queued\": true}', 'running', "
        "now() - interval '3 hours')",
        str(uuid.uuid4()),
        tenant,
        connection,
    )
    status = (
        await client.get(f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers)
    ).json()
    assert status["status"] == "failed"
    assert status["error"] == "The reload did not finish. Try again."


def test_parse_month() -> None:
    assert reload.parse_month("2026-10") == date(2026, 10, 1)
    assert reload.next_allowed(None) is None


def test_overall_status() -> None:
    oct_, nov = date(2026, 10, 1), date(2026, 11, 1)
    done = reload.PeriodResult(oct_, "reloaded")
    failed = reload.PeriodResult(nov, "failed", detail="x")
    gone = reload.PeriodResult(nov, "not_in_bucket")
    status = reload._status
    assert status(reload.ReloadResult("succeeded", periods=[done, failed]), None) == "partial"
    assert status(reload.ReloadResult("succeeded", periods=[failed]), None) == "failed"
    # A month gone from the bucket counts only when that month was asked for.
    assert status(reload.ReloadResult("succeeded", periods=[done, gone]), None) == "succeeded"
    assert status(reload.ReloadResult("succeeded", periods=[done, gone]), nov) == "partial"
    assert status(reload.ReloadResult("succeeded", errors=["bad manifest"]), None) == "failed"
    assert status(reload.ReloadResult("succeeded"), None) == "failed"  # nothing in the bucket


async def test_a_connection_that_stopped_working_is_not_reloaded(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET status = 'failed' WHERE id = %s", connection)
    result = await reload.reload_connection(uuid.UUID(tenant), uuid.UUID(connection))
    assert result.status == "skipped"
    status = (
        await client.get(f"/v1/tenants/{tenant}/connections/{connection}/reload", headers=headers)
    ).json()
    assert status["status"] == "failed"
    assert status["error"] == "The connection is not working, or has no export. Check it first."
