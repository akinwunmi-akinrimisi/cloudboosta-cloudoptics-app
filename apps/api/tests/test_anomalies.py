"""Spend anomalies (PRD US-6.3, FR-ANA-08, WBS AN-11)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from httpx import AsyncClient
from test_budgets import ACCOUNT, OTHER, workspace
from test_invites import Outbox, outbox  # noqa: F401 (fixture)

from cloudoptics.anomalies import jobs, service
from cloudoptics.platform.db import get_sessionmaker, scope

# Relative to today, so the list's 90-day window keeps the test data in view.
_back = date.today() - timedelta(days=50)
START = _back - timedelta(days=_back.weekday())  # a Monday about seven weeks ago
SPIKE = START + timedelta(days=35)  # the Monday five weeks later


def cost(
    owner_sql, tenant: dict[str, Any], connection: str, day: date, usd: float,
    account: str = ACCOUNT, service_code: str = "AmazonEC2",
    usage_type: str = "EUW2-BoxUsage:t3.micro", category: str = "Usage",
) -> None:  # fmt: skip
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, region_id, usage_type, "
        "charge_category, billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Compute', 'eu-west-2', %s, %s, %s, %s, %s, 'v1')",
        tenant["id"],
        connection,
        day,
        day.replace(day=1),
        account,
        service_code,
        {"AmazonEC2": "Amazon EC2", "AmazonS3": "Amazon S3"}[service_code],
        usage_type,
        category,
        usd,
        usd,
        usd,
    )


def steady(owner_sql, tenant, connection, until: date) -> None:  # type: ignore[no-untyped-def]
    """USD 20 a day of EC2 and USD 5 of S3 from START to ``until``, a little higher on
    weekdays so the weekday baseline matters."""
    day = START
    while day <= until:
        weekday = day.weekday() < 5
        cost(owner_sql, tenant, connection, day, 20 if weekday else 12)
        cost(owner_sql, tenant, connection, day, 5, service_code="AmazonS3", usage_type="Storage")
        day += timedelta(days=1)


async def run(tenant_id: str, today: date) -> int:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await service.detect_and_alert(session, tid, today)


def test_threshold_needs_both_the_percent_and_the_minimum() -> None:
    assert service.baseline([10, 10, 12, 30]) == (11, 1)
    # 3 x MAD is 3, 30% of 11 is 3.3: above 14.3 and at least the minimum.
    assert service.threshold(11, 1, 30) == 14.3
    assert service.is_anomaly(100, 11, 1, 30, 50)
    assert not service.is_anomaly(60, 11, 1, 30, 50)  # 49 above: under the minimum
    assert not service.is_anomaly(1250, 1000, 0, 30, 50)  # 25% above: under the percent
    assert service.is_anomaly(1350, 1000, 0, 30, 50)
    assert not service.is_anomaly(1350, 1000, 120, 30, 50)  # noisy: 3 x MAD is 360
    assert service.is_anomaly(60, 0, 0, 30, 50)  # new spend


async def test_a_spike_is_found_explained_alerted_once_and_triaged(
    client: AsyncClient,
    login,
    owner_sql,
    outbox: Outbox,  # noqa: F811
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    steady(owner_sql, tenant, connection, SPIKE)
    # The spike: a big instance on a Monday, and a little in the other account's bill.
    cost(owner_sql, tenant, connection, SPIKE, 150, usage_type="EUW2-BoxUsage:m5.4xlarge")
    cost(owner_sql, tenant, connection, SPIKE, 15, usage_type="EUW2-DataTransfer-Out-Bytes")
    cost(owner_sql, tenant, connection, SPIKE, 3, account=OTHER)
    # Credits and tax are not spend.
    cost(owner_sql, tenant, connection, SPIKE - timedelta(days=1), -500, category="Credit")
    cost(owner_sql, tenant, connection, SPIKE - timedelta(days=1), 300, category="Tax")

    assert await run(tid, SPIKE) == 1
    [mail] = outbox.sent
    assert mail.subject == (
        f"Unusual spend: Amazon EC2 in {ACCOUNT} is USD 165.00 above normal on {SPIKE:%d %b}"
    )
    assert "USD 185.00 against USD 20.00 expected (USD 165.00 above, +825%)" in mail.text
    assert "EUW2-BoxUsage:m5.4xlarge (eu-west-2) +USD 150.00" in mail.text
    me = (await client.get("/v1/me", headers=admin)).json()
    assert mail.to == [me["email"].lower()]
    assert await run(tid, SPIKE) == 0  # once per anomaly

    base = f"/v1/tenants/{tid}/anomalies"
    [found] = (await client.get(base, headers=admin)).json()
    assert (found["date"], found["accountId"], found["serviceCode"]) == (
        SPIKE.isoformat(),
        ACCOUNT,
        "AmazonEC2",
    )
    assert (found["actualUsd"], found["expectedUsd"], found["impactUsd"]) == (185, 20, 165)
    assert found["deviationPercent"] == 825 and found["status"] == "open" and found["alerted"]
    assert [c["usageType"] for c in found["contributors"]] == [
        "EUW2-BoxUsage:m5.4xlarge",
        "EUW2-DataTransfer-Out-Bytes",
    ]

    detail = (await client.get(f"{base}/{found['id']}", headers=admin)).json()
    points = {p["date"]: p for p in detail["series"]}
    assert len(detail["series"]) == 28  # four weeks to the spike, no data after it
    assert points[SPIKE.isoformat()] == {
        "date": SPIKE.isoformat(),
        "actual": 185,
        "expected": 20,
        "upper": 26,
    }
    # The chart's first day has no four weeks of bills before it.
    assert points[(SPIKE - timedelta(days=27)).isoformat()]["expected"] is None

    viewer_email = f"viewer-{uuid.uuid4().hex[:6]}@example.com"
    viewer = await login(viewer_email)
    await client.get("/v1/me", headers=viewer)
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) SELECT %s, id, 'viewer' "
        "FROM app_user WHERE email = %s",
        tid,
        viewer_email,
    )
    assert len((await client.get(base, headers=viewer)).json()) == 1
    change = {"status": "expected", "note": " Load test "}
    r = await client.patch(f"{base}/{found['id']}", json=change, headers=viewer)
    assert r.status_code == 403
    r = await client.patch(f"{base}/{found['id']}", json=change, headers=admin)
    assert (r.json()["status"], r.json()["note"]) == ("expected", "Load test")
    assert r.json()["statusChangedAt"]
    assert (await client.get(base, params={"status": "open"}, headers=admin)).json() == []
    actions = owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s AND action LIKE 'anomaly.%%'", tid
    )
    assert actions == [("anomaly.status_changed",)]

    # Marked expected: the same scope's spike the next day is stored but sends nothing.
    nxt = SPIKE + timedelta(days=1)
    cost(owner_sql, tenant, connection, nxt, 20)
    cost(owner_sql, tenant, connection, nxt, 150, usage_type="EUW2-BoxUsage:m5.4xlarge")
    cost(owner_sql, tenant, connection, nxt, 5, service_code="AmazonS3", usage_type="Storage")
    assert await run(tid, nxt) == 0
    rows = (await client.get(base, headers=admin)).json()
    assert [(a["date"], a["status"], a["suppressed"]) for a in rows] == [
        (nxt.isoformat(), "expected", True),
        (SPIKE.isoformat(), "expected", False),
    ]

    other, other_admin, _ = await workspace(client, login, owner_sql)
    r = await client.get(f"/v1/tenants/{other['id']}/anomalies/{found['id']}", headers=other_admin)
    assert r.status_code == 404


async def test_settings_raise_the_bar_and_short_history_finds_nothing(
    client: AsyncClient,
    login,
    owner_sql,
    outbox: Outbox,  # noqa: F811
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    url = f"/v1/tenants/{tid}/alert-settings"
    view = (await client.get(url, headers=admin)).json()
    assert view["settings"] == {
        "anomalyMinImpact": 50,
        "anomalyMinImpactCurrency": "USD",
        "anomalyMinPercent": 30,
        "anomalyEmails": [],
    }
    assert view["minImpactUsd"] == 50

    # Under four weeks of bills: no baseline yet.
    steady(owner_sql, tenant, connection, START + timedelta(days=20))
    cost(owner_sql, tenant, connection, START + timedelta(days=21), 900)
    assert await run(tid, START + timedelta(days=21)) == 0

    r = await client.put(
        url,
        json={"anomalyMinImpact": 200, "anomalyMinPercent": 50,
              "anomalyEmails": ["Ops@Example.com"]},
        headers=admin,
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["anomalyEmails"] == ["ops@example.com"]
    # Replace that 900 with steady days to SPIKE; a USD 150 jump is then under the new
    # USD 200 minimum.
    owner_sql(
        "DELETE FROM cost_daily WHERE tenant_id = %s AND usage_date > %s",
        tid,
        START + timedelta(days=20),
    )
    day = START + timedelta(days=21)
    while day <= SPIKE:
        cost(owner_sql, tenant, connection, day, 20 if day.weekday() < 5 else 12)
        day += timedelta(days=1)
    cost(owner_sql, tenant, connection, SPIKE, 150)
    assert await run(tid, SPIKE) == 0
    cost(owner_sql, tenant, connection, SPIKE, 100)
    assert await run(tid, SPIKE) == 1
    assert "ops@example.com" in outbox.sent[-1].to

    assert (
        await client.put(url, json={"anomalyMinImpactCurrency": "EUR"}, headers=admin)
    ).status_code == 422
    r = await client.put(
        url, json={"anomalyMinImpact": 80000, "anomalyMinImpactCurrency": "NGN"}, headers=admin
    )
    assert r.json()["minImpactUsd"] is None  # no NGN rate yet: the USD default applies
    assert (await client.put(url, json={"anomalyMinPercent": 5}, headers=admin)).status_code == 422
    actions = owner_sql(
        "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action = 'alert_settings.updated'",
        tid,
    )
    assert actions == [(2,)]


async def test_failed_email_is_tried_again(client: AsyncClient, login, owner_sql) -> None:
    from cloudoptics.notifications.email import set_mailer

    tenant, _, connection = await workspace(client, login, owner_sql)
    steady(owner_sql, tenant, connection, SPIKE)
    cost(owner_sql, tenant, connection, SPIKE, 400)
    set_mailer(Outbox(fail=True))
    try:
        assert await run(tenant["id"], SPIKE) == 0
        working = Outbox()
        set_mailer(working)
        assert await run(tenant["id"], SPIKE) == 1
        assert await run(tenant["id"], SPIKE) == 0
    finally:
        set_mailer(None)


async def test_new_bills_and_the_hourly_poll_queue_a_detection(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs

    tenant, _, _ = await workspace(client, login, owner_sql)
    queued: list[str] = []

    async def fake_defer(app, tenant_id):  # type: ignore[no-untyped-def]
        queued.append(tenant_id)

    monkeypatch.setattr(jobs, "defer_detect", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.poll(0) == len(queued)
    assert tenant["id"] in queued
    assert await jobs.detect(tenant["id"]) == 0  # no bills yet
