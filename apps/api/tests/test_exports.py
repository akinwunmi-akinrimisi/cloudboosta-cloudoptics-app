"""CSV exports and their async operation (PRD US-7.2, docs/09 §3.2, WBS API-7)."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from test_anomalies import cost
from test_budgets import OTHER, workspace
from test_digest import member, recommendation

from cloudoptics.exports import jobs, service
from cloudoptics.exports import routes as export_routes
from cloudoptics.platform.settings import get_settings

SEP = date(2026, 9, 1)
ACCOUNT = "111122223333"


def rows_of(body: str) -> list[list[str]]:
    assert body.startswith("\ufeff")  # spreadsheets read UTF-8 accents with the mark
    return list(csv.reader(io.StringIO(body[1:])))


def as_dicts(body: str) -> list[dict[str, str]]:
    header, *rows = rows_of(body)
    return [dict(zip(header, r, strict=True)) for r in rows]


async def export(
    client: AsyncClient, tid: str, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    r = await client.post(f"/v1/tenants/{tid}/exports", json=body, headers=headers)
    assert r.status_code in (201, 202), r.text
    return dict(r.json())


async def download(client: AsyncClient, url: str, headers: dict[str, str]) -> str:
    r = await client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    return r.content.decode("utf-8")


def tagged_cost(owner_sql, tenant, connection, day: date, usd: float, tags: dict[str, str]):  # type: ignore[no-untyped-def]
    digest = uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(tags, sort_keys=True)).bytes
    owner_sql(
        "INSERT INTO tag_set (tenant_id, hash, tags) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        tenant["id"],
        digest,
        json.dumps(tags),
    )
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "provider_account_id, service_code, service_name, service_category, region_id, usage_type, "
        "charge_category, tag_set_hash, billed_cost_usd, effective_cost_usd, net_cost_usd, "
        "data_version) VALUES (%s, %s, %s, %s, %s, 'AmazonS3', 'Amazon S3', 'Storage', "
        "'eu-west-2', 'TimedStorage', 'Usage', %s, %s, %s, %s, 'v1')",
        tenant["id"],
        connection,
        day,
        day.replace(day=1),
        OTHER,
        digest,
        usd,
        usd,
        usd,
    )


def test_cells_that_look_like_formulas_are_neutralised() -> None:
    for risky in ("=1+1", "+1", "-2", "@SUM(A1)", "\tx", "\rx", "\nx", "  =HYPERLINK(1)"):
        assert service.cell(risky) == "'" + risky
    assert service.cell("Amazon S3") == "Amazon S3"
    assert service.cell("") == ""
    # Numbers stay numbers: a credit is negative, and is not text.
    assert service.cell(-20.0) == "-20.00"
    assert service.cell(1.23456, 4) == "1.2346"
    assert service.cell(3) == "3"
    assert service.cell(None) == "" and service.cell(True) == "true"
    assert service.cell(datetime(2026, 9, 1, 10, tzinfo=UTC)) == "2026-09-01T10:00:00Z"
    assert service.evidence_summary(
        [{"kind": "state", "value": "available", "sinceDays": 12.5}, {"kind": "x", "value": None}]
    ) == ("state: available (sinceDays=12.5); x:")
    assert service.content_disposition('a"b.csv') == 'attachment; filename="cloudoptics-export.csv"'


async def test_costs_export_by_service_account_and_tag_in_both_currencies(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql, country="ZA")
    tid = tenant["id"]
    me = (await client.get("/v1/me", headers=admin)).json()
    # A hostile account name and tag value: both come from the customer's AWS.
    owner_sql(
        "UPDATE cloud_account SET name = %s WHERE tenant_id = %s AND provider_account_id = %s",
        '=HYPERLINK("http://evil")',
        tid,
        OTHER,
    )
    for i in range(3):
        day = SEP + timedelta(days=i)
        cost(owner_sql, tenant, connection, day, 10)
        tagged_cost(owner_sql, tenant, connection, day, 2, {"team": "@payments", "env": "prod"})
        owner_sql(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
            "VALUES (gen_random_uuid(), 'ZAR', %s, 'official', %s, 'manual', %s)",
            day,
            18 + i,
            me["id"],
        )
    cost(owner_sql, tenant, connection, SEP + timedelta(days=1), -5, category="Credit")
    cost(owner_sql, tenant, connection, SEP + timedelta(days=2), 30, category="Tax")
    # October has spend but no rate: no local amount rather than a wrong one.
    cost(owner_sql, tenant, connection, date(2026, 10, 1), 7)

    done = await export(
        client,
        tid,
        admin,
        kind="costs",
        start="2026-09-01",
        end="2026-10-15",
        groupBy=["service", "account", "tag"],
        tagKey="team",
    )
    assert done["status"] == "succeeded" and done["progress"] == 100
    assert done["rowCount"] == 3 and done["fileName"] == "cloudoptics-costs-20260901-20261015.csv"
    assert done["params"]["groupBy"] == ["service", "account", "tag"]
    assert done["downloadUrl"] == f"/v1/tenants/{tid}/exports/{done['id']}/download"
    body = await download(client, done["downloadUrl"], admin)
    header, *rows = rows_of(body)
    assert header == [
        "period_start", "period_end", "service_code", "service_name", "account_id",
        "account_name", "tag:team", "amortised_cost_usd", "billed_cost_usd",
        "amortised_cost_zar", "fx_rate_avg",
    ]  # fmt: skip
    assert rows == [
        # EC2: 30 - 5 credit (tax left out) at 18, 19, 20: 180 + 95 + 200.
        ["2026-09-01", "2026-09-30", "AmazonEC2", "Amazon EC2", ACCOUNT, ACCOUNT, "",
         "25.00", "25.00", "475.00", "19.0000"],
        ["2026-09-01", "2026-09-30", "AmazonS3", "Amazon S3", OTHER,
         "'=HYPERLINK(\"http://evil\")", "'@payments", "6.00", "6.00", "114.00", "19.0000"],
        ["2026-10-01", "2026-10-15", "AmazonEC2", "Amazon EC2", ACCOUNT, ACCOUNT, "",
         "7.00", "7.00", "", ""],
    ]  # fmt: skip

    # By day, one account, with tax; no dimension.
    daily = await export(
        client,
        tid,
        admin,
        kind="costs",
        start="2026-09-01",
        end="2026-09-30",
        granularity="day",
        groupBy=[],
        accountId=ACCOUNT,
        includeTax=True,
    )
    lines = as_dicts(await download(client, daily["downloadUrl"], admin))
    assert [(r["period_start"], r["amortised_cost_usd"]) for r in lines] == [
        ("2026-09-01", "10.00"),
        ("2026-09-02", "5.00"),
        ("2026-09-03", "40.00"),
    ]

    # Without gzip the stored file is inflated on the way out.
    r = await client.get(daily["downloadUrl"], headers={**admin, "Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert r.content.decode("utf-8").count("\r\n") == 4
    assert r.headers["content-disposition"] == (
        'attachment; filename="cloudoptics-costs-20260901-20260930.csv"'
    )

    base = f"/v1/tenants/{tid}/exports"
    for bad in (
        {"kind": "costs", "start": "2026-09-02", "end": "2026-09-01"},
        {"kind": "costs", "start": "2025-01-01", "end": "2026-09-01"},
        {"kind": "costs", "start": "2026-09-01", "end": "2026-09-02", "groupBy": ["tag"]},
        {"kind": "costs", "start": "2026-09-01", "end": "2026-09-02", "tagKey": "team"},
        {"kind": "costs", "start": "2026-09-01", "end": "2026-09-02", "accountId": "not an id"},
        {"kind": "invoices"},
    ):
        assert (await client.post(base, json=bad, headers=admin)).status_code == 422, bad


async def test_recommendation_anomaly_and_budget_exports(
    client: AsyncClient, login, owner_sql
) -> None:
    # Ghana: the exchange rate table is shared by every test, so a currency no other uses.
    tenant, admin, connection = await workspace(client, login, owner_sql, country="GH")
    tid = tenant["id"]
    me = (await client.get("/v1/me", headers=admin)).json()
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
        "VALUES (gen_random_uuid(), 'GHS', %s, 'official', 15, 'manual', %s)",
        datetime.now(UTC).date(),
        me["id"],
    )
    when = datetime(2026, 9, 15, 10, tzinfo=UTC)
    first = recommendation(owner_sql, tenant, "=cmd|' /C calc'!A0", 40, when)
    owner_sql(
        "UPDATE recommendation SET evidence = %s WHERE id = %s",
        json.dumps([{"kind": "state", "value": "available", "sinceDays": 12.5}]),
        first,
    )
    recommendation(owner_sql, tenant, "Old snapshot", 5, when, status="done")
    _, viewer = await member(client, login, owner_sql, tenant, "viewer")

    # Viewers can export: the list is what they can already see.
    rec = await export(client, tid, viewer, kind="recommendations")
    lines = as_dicts(await download(client, rec["downloadUrl"], viewer))
    assert len(lines) == 1
    line = lines[0]
    assert line["title"] == "'=cmd|' /C calc'!A0"
    assert line["status"] == "new" and line["savings_usd_month"] == "40.00"
    assert line["savings_ghs_month"] == "600.00"  # at today's rate
    assert line["evidence_summary"] == "state: available (sinceDays=12.5)"
    assert line["account_id"] == "111122223333" and line["risk_level"] == "low"
    every = await export(client, tid, viewer, kind="recommendations", status="all")
    assert every["rowCount"] == 2

    owner_sql(
        "INSERT INTO anomaly (id, tenant_id, usage_date, "
        "provider_account_id, service_code, service_name, "
        "actual_usd, expected_usd, mad_usd, impact_usd, contributors, note, engine_version) "
        "VALUES (gen_random_uuid(), %s, %s, %s, 'AmazonEC2', 'Amazon EC2', 120, 20, 2, 100, "
        "%s, %s, 'test')",
        tid,
        datetime.now(UTC).date() - timedelta(days=3),
        ACCOUNT,
        json.dumps(
            [
                {
                    "usageType": "BoxUsage:m5.large",
                    "region": "eu-west-2",
                    "actualUsd": 110,
                    "expectedUsd": 10,
                    "impactUsd": 100,
                }
            ]
        ),
        "+ load test",
    )
    anomalies = await export(client, tid, viewer, kind="anomalies", days=30)
    assert anomalies["fileName"].startswith("cloudoptics-anomalies-")
    (row,) = as_dicts(await download(client, anomalies["downloadUrl"], viewer))
    assert row["impact_usd"] == "100.00" and row["deviation_percent"] == "500.0"
    assert row["note"] == "'+ load test"
    assert row["top_usage_types"] == "BoxUsage:m5.large +100.00"
    resolved = await export(client, tid, viewer, kind="anomalies", status="resolved")
    assert resolved["rowCount"] == 0

    r = await client.post(
        f"/v1/tenants/{tid}/budgets", json={"name": "-Company", "amount": 500}, headers=admin
    )
    assert r.status_code == 201, r.text
    budgets = await export(client, tid, viewer, kind="budgets")
    (row,) = as_dicts(await download(client, budgets["downloadUrl"], viewer))
    assert row["name"] == "'-Company" and row["amount"] == "500.00"
    assert row["scope"] == "workspace" and row["currency"] == "USD"
    assert row["thresholds"].startswith("actual ")

    # Each export is written to the audit log.
    log = (await client.get(f"/v1/tenants/{tid}/audit-log", headers=admin)).json()
    created = [e for e in log["entries"] if e["action"] == "export.created"]
    assert len(created) == 5
    assert {e["after"]["kind"] for e in created} == {"recommendations", "anomalies", "budgets"}
    assert connection


async def test_exports_belong_to_their_maker_and_workspace(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, _ = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    mine = await export(client, tid, admin, kind="budgets")
    url = f"/v1/tenants/{tid}/exports/{mine['id']}"

    # Another member of the same workspace cannot see or download it.
    _, colleague = await member(client, login, owner_sql, tenant, "member")
    assert (await client.get(url, headers=colleague)).status_code == 404
    assert (await client.get(f"{url}/download", headers=colleague)).status_code == 404
    listed = (await client.get(f"/v1/tenants/{tid}/exports", headers=colleague)).json()
    assert listed["exports"] == []

    # Someone from another workspace gets nothing, through either workspace's path.
    other, outsider, _ = await workspace(client, login, owner_sql)
    assert (await client.get(url, headers=outsider)).status_code == 404
    assert (await client.get(f"{url}/download", headers=outsider)).status_code == 404
    theirs = f"/v1/tenants/{other['id']}/exports/{mine['id']}"
    assert (await client.get(theirs, headers=outsider)).status_code == 404
    assert (await client.get(f"{theirs}/download", headers=outsider)).status_code == 404
    assert (
        await client.post(f"/v1/tenants/{tid}/exports", json={"kind": "budgets"}, headers=outsider)
    ).status_code == 404

    # The maker sees it; once removed from the workspace, the download stops working.
    listed = (await client.get(f"/v1/tenants/{tid}/exports", headers=admin)).json()
    assert [e["id"] for e in listed["exports"]] == [mine["id"]]
    _, leaver = await member(client, login, owner_sql, tenant, "viewer")
    gone = await export(client, tid, leaver, kind="budgets")
    owner_sql(
        "DELETE FROM membership WHERE tenant_id = %s AND user_id = "
        "(SELECT requested_by FROM export_job WHERE id = %s)",
        tid,
        gone["id"],
    )
    gone_url = f"/v1/tenants/{tid}/exports/{gone['id']}/download"
    assert (await client.get(gone_url, headers=leaver)).status_code == 404


async def test_exports_expire_after_seven_days(client: AsyncClient, login, owner_sql) -> None:
    tenant, admin, _ = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    old = await export(client, tid, admin, kind="budgets")
    fresh = await export(client, tid, admin, kind="budgets")
    created = datetime.fromisoformat(old["createdAt"])
    assert datetime.fromisoformat(old["expiresAt"]) - created == timedelta(days=7)
    owner_sql(
        "UPDATE export_job SET expires_at = now() - interval '1 minute' WHERE id = %s", old["id"]
    )
    url = f"/v1/tenants/{tid}/exports/{old['id']}"
    view = (await client.get(url, headers=admin)).json()
    assert view["status"] == "expired" and view["downloadUrl"] is None
    assert (await client.get(f"{url}/download", headers=admin)).status_code == 410
    listed = (await client.get(f"/v1/tenants/{tid}/exports", headers=admin)).json()
    assert [e["id"] for e in listed["exports"]] == [fresh["id"]]

    # The daily clean-up deletes expired files only, across workspaces.
    assert await jobs.expire(0) >= 1
    assert owner_sql("SELECT count(*) FROM export_job WHERE id = %s", old["id"]) == [(0,)]
    assert (await client.get(url, headers=admin)).status_code == 404
    assert owner_sql("SELECT count(*) FROM export_job WHERE id = %s", fresh["id"]) == [(1,)]


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Queue mode with a tiny sync limit: exports over one row go to the worker."""
    deferred: list[tuple[str, str]] = []

    async def fake_defer(tenant_id: str, export_id: str) -> None:
        deferred.append((tenant_id, export_id))

    monkeypatch.setattr(jobs, "defer_generate", fake_defer)
    monkeypatch.setattr(service, "SYNC_ROWS", 1)
    monkeypatch.setattr(service, "PROGRESS_EVERY", 1)
    monkeypatch.setattr(get_settings(), "jobs_mode", "queue")
    return deferred


async def test_large_exports_run_on_the_worker_with_progress(
    client: AsyncClient, login, owner_sql, queued: list[tuple[str, str]], monkeypatch
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    for i in range(4):
        cost(owner_sql, tenant, connection, SEP + timedelta(days=i), 10 + i)
    body = {"kind": "costs", "start": "2026-09-01", "end": "2026-09-30", "granularity": "day"}
    r = await client.post(f"/v1/tenants/{tid}/exports", json=body, headers=admin)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["progress"] == 0 and job["downloadUrl"] is None
    assert queued == [(tid, job["id"])]
    url = f"/v1/tenants/{tid}/exports/{job['id']}"
    assert (await client.get(f"{url}/download", headers=admin)).status_code == 409

    seen: list[int] = []
    real_build = service.build

    async def watched(session, tenant_id, spec, **kwargs):  # type: ignore[no-untyped-def]
        progress = kwargs["progress"]

        async def spy(share: float) -> None:
            await progress(share)
            seen.append((await client.get(url, headers=admin)).json()["progress"])

        return await real_build(session, tenant_id, spec, total=kwargs["total"], progress=spy)

    monkeypatch.setattr(service, "build", watched)
    assert await jobs.generate(tid, job["id"]) == "succeeded"
    assert seen == [27, 50, 72, 95]  # committed as it goes, so the app can show it
    done = (await client.get(url, headers=admin)).json()
    assert done["status"] == "succeeded" and done["progress"] == 100 and done["rowCount"] == 4
    lines = as_dicts(await download(client, done["downloadUrl"], admin))
    assert [r["amortised_cost_usd"] for r in lines] == ["10.00", "11.00", "12.00", "13.00"]
    # Running it again (a retry) changes nothing.
    assert await jobs.generate(tid, job["id"]) == "succeeded"

    # Too big to keep: the export fails with a reason the user can act on.
    monkeypatch.setattr(service, "build", real_build)
    monkeypatch.setattr(service, "MAX_BYTES", 10)
    r = await client.post(f"/v1/tenants/{tid}/exports", json=body, headers=admin)
    failed_id = r.json()["id"]
    assert await jobs.generate(tid, failed_id) == "failed"
    failed = (await client.get(f"/v1/tenants/{tid}/exports/{failed_id}", headers=admin)).json()
    assert failed["status"] == "failed" and "too large" in failed["error"]

    # Any other error is logged and shown plainly.
    async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "build", broken)
    r = await client.post(f"/v1/tenants/{tid}/exports", json=body, headers=admin)
    assert await jobs.generate(tid, r.json()["id"]) == "failed"
    failed = (await client.get(f"/v1/tenants/{tid}/exports/{r.json()['id']}", headers=admin)).json()
    assert failed["error"].startswith("The export failed.")

    # Queued before the request committed: the job tries again.
    with pytest.raises(jobs.ExportNotCommittedError):
        await jobs.generate(tid, str(uuid.uuid4()))


async def test_exports_are_rate_limited_and_capped(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    cost(owner_sql, tenant, connection, SEP, 10)
    cost(owner_sql, tenant, connection, SEP, 3, service_code="AmazonS3")
    base = f"/v1/tenants/{tid}/exports"
    body = {"kind": "costs", "start": "2026-09-01", "end": "2026-09-30"}

    monkeypatch.setattr(service, "MAX_ROWS", 1)
    r = await client.post(base, json=body, headers=admin)
    assert r.status_code == 422 and r.json()["code"] == "export_too_large"
    # Too large even when the count passed (a sync export checks as it writes).
    monkeypatch.setattr(service, "MAX_BYTES", 10)
    monkeypatch.setattr(service, "MAX_ROWS", 10)
    r = await client.post(base, json=body, headers=admin)
    assert r.status_code == 422 and "too large" in r.json()["title"]
    monkeypatch.setattr(service, "MAX_BYTES", 50 * 1024 * 1024)

    for _ in range(export_routes.EXPORTS_PER_HOUR):
        assert (await client.post(base, json=body, headers=admin)).status_code == 201
    r = await client.post(base, json=body, headers=admin)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 3600
    # The limit is per person: a colleague can still export.
    _, colleague = await member(client, login, owner_sql, tenant, "viewer")
    assert (await client.post(base, json=body, headers=colleague)).status_code == 201
