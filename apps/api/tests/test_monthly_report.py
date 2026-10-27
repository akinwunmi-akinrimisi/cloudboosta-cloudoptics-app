"""Monthly executive report (PRD US-7.1, FR-FX-05, WBS ADM-9)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from test_anomalies import cost
from test_budgets import workspace
from test_digest import member, recommendation
from test_invites import Outbox, outbox  # noqa: F401 (fixture)

from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.reporting import monthly

AUG, SEP = date(2026, 8, 1), date(2026, 9, 1)
DUE = datetime(2026, 10, 5, 5, 30, tzinfo=UTC)  # 06:30 in Lagos, Monday 5 Oct


def test_the_report_is_due_on_the_third_working_day_from_six_local() -> None:
    assert monthly.working_day(date(2026, 10, 1)) == date(2026, 10, 5)  # Thu, Fri, Mon
    assert monthly.working_day(date(2026, 11, 1)) == date(2026, 11, 4)  # starts on a Sunday
    assert monthly.due(DUE, "Africa/Lagos") == SEP
    assert monthly.due(DUE - timedelta(hours=1), "Africa/Lagos") is None  # 05:30
    assert monthly.due(DUE + timedelta(days=1), "Africa/Lagos") is None
    assert monthly.previous_month(date(2026, 1, 15)) == date(2025, 12, 1)
    assert monthly.month_end(date(2026, 2, 1)) == date(2026, 2, 28)


async def run(tenant_id: str, now: datetime) -> int:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await monthly.send_due(session, tid, now)


async def test_the_report_is_made_once_emailed_and_kept(
    client: AsyncClient,
    login,
    owner_sql,
    outbox: Outbox,  # noqa: F811
) -> None:
    # Egypt: the exchange rate table is shared by every test, so a currency no other uses
    # on these dates.
    tenant, admin, connection = await workspace(client, login, owner_sql, country="EG")
    tid = tenant["id"]
    owner_sql("UPDATE tenant SET time_zone = 'Africa/Lagos' WHERE id = %s", tid)
    me = (await client.get("/v1/me", headers=admin)).json()
    # August: USD 10 a day of EC2 at EGP 50. September: 15 a day of EC2 and 2 of S3 at
    # EGP 48, a USD 20 credit and USD 30 of tax.
    for i in range(31):
        day = AUG + timedelta(days=i)
        cost(owner_sql, tenant, connection, day, 10)
        owner_sql(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
            "VALUES (gen_random_uuid(), 'EGP', %s, 'official', %s, 'manual', %s)",
            day,
            50 if day < SEP else 48,
            me["id"],
        )
    for i in range(30):
        day = SEP + timedelta(days=i)
        cost(owner_sql, tenant, connection, day, 15)
        cost(owner_sql, tenant, connection, day, 2, service_code="AmazonS3", usage_type="Storage")
        owner_sql(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
            "VALUES (gen_random_uuid(), 'EGP', %s, 'official', 48, 'manual', %s)",
            day,
            me["id"],
        )
    cost(owner_sql, tenant, connection, date(2026, 9, 10), -20, category="Credit")
    cost(owner_sql, tenant, connection, date(2026, 9, 30), 30, category="Tax")
    await client.post(
        f"/v1/tenants/{tid}/budgets", json={"name": "Company", "amount": 500}, headers=admin
    )
    when = datetime(2026, 9, 15, 10, tzinfo=UTC)
    recommendation(owner_sql, tenant, "Delete volume build-cache", 40, when)
    done = recommendation(owner_sql, tenant, "Old snapshot", 5, when, status="done")
    owner_sql(
        "INSERT INTO recommendation_event (id, tenant_id, recommendation_id, kind, from_status, "
        "to_status, created_at) VALUES (gen_random_uuid(), %s, %s, 'status_changed', 'new', "
        "'done', %s)",
        tid,
        done,
        when,
    )

    viewer_email, viewer = await member(client, login, owner_sql, tenant, "viewer")
    _, quiet = await member(client, login, owner_sql, tenant, "member")
    r = await client.put(
        f"/v1/tenants/{tid}/notification-preferences/me",
        json={"monthlyReport": False},
        headers=quiet,
    )
    assert r.json()["mine"]["monthlyReport"] is False
    settings = f"/v1/tenants/{tid}/reports/settings"
    assert (
        await client.put(settings, json={"emails": ["board@example.com"]}, headers=viewer)
    ).status_code == 403
    r = await client.put(settings, json={"emails": ["Board@Example.com"]}, headers=admin)
    assert r.json() == {"emails": ["board@example.com"]}

    assert await run(tid, DUE - timedelta(hours=1)) == 0  # not yet
    assert await run(tid, DUE) == 3
    assert sorted(m.to[0] for m in outbox.sent) == sorted(
        [me["email"].lower(), viewer_email, "board@example.com"]
    )
    mail = outbox.sent[0]
    # 450 + 60 - 20 + 30 = 520 against 310; EGP 24,960 against 15,500.
    assert mail.subject == (
        "Acme Pay: AWS spend for September 2026 was USD 520.00 (EGP 24,960) "
        "(+68% on the month before)"
    )
    text_ = mail.text
    assert "the month before USD 310.00 (EGP 15,500)." in text_
    # Usage: 210 x 50 = +10,500; rate: 520 x (48 - 50) = -1,040.
    assert "Change in EGP: +10,500 from usage, -1,040 from the exchange rate." in text_
    assert "AWS credits applied: USD 20.00." in text_
    assert "- Amazon EC2: +USD 120.00 (USD 430.00 this month)" in text_
    assert "- Amazon S3: +USD 60.00" in text_
    assert "Company (USD 500): over budget, 104% spent" in text_
    assert "1 open recommendations worth USD 40.00 a month (1 found this month)." in text_
    assert "Marked done this month: 1, saving USD 5.00 a month." in text_
    assert "month average EGP 48.00 per USD" in text_
    assert "/reports?month=2026-09" in text_
    assert await run(tid, DUE + timedelta(hours=1)) == 0  # once

    base = f"/v1/tenants/{tid}/reports"
    listing = (await client.get(base, headers=viewer)).json()
    assert [(r["month"], r["spendUsd"], r["spendLocal"]) for r in listing["reports"]] == [
        ("2026-09", 520, 24960)
    ]
    view = (await client.get(f"{base}/2026-09", headers=viewer)).json()
    assert view["stored"] is True
    report = view["report"]
    assert report["spend"]["usageEffect"] == 10500 and report["spend"]["fxEffect"] == -1040
    assert [d["serviceName"] for d in report["drivers"]] == ["Amazon EC2", "Amazon S3"]
    assert report["accuracy"]["dataThrough"] == "2026-09-30"
    # Stored figures stay as they were when the data changes.
    cost(owner_sql, tenant, connection, date(2026, 9, 20), 1000)
    view = (await client.get(f"{base}/2026-09", headers=viewer)).json()
    assert view["report"]["spend"]["usd"] == 520
    # August has no report yet: worked out now, with no month before it.
    august = (await client.get(f"{base}/2026-08", headers=viewer)).json()
    assert august["stored"] is False and august["report"]["spend"]["changePercent"] is None
    assert (await client.get(f"{base}/2026-07", headers=viewer)).status_code == 404
    assert (await client.get(f"{base}/2026-13", headers=viewer)).status_code == 422
    this_month = f"{datetime.now(UTC):%Y-%m}"
    assert (await client.get(f"{base}/{this_month}", headers=viewer)).status_code == 422

    # An admin makes it again from today's data and sends it to everyone.
    assert (await client.post(f"{base}/2026-09/send", headers=viewer)).status_code == 403
    r = await client.post(f"{base}/2026-09/send", headers=admin)
    assert r.json()["sentTo"] == 3 and r.json()["report"]["spend"]["usd"] == 1520
    assert len(outbox.sent) == 6
    actions = owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s AND action LIKE 'report%%' ORDER BY id",
        tid,
    )
    assert actions == [("report_settings.updated",), ("report.sent",)]


async def test_no_bills_no_report(client: AsyncClient, login, owner_sql) -> None:
    tenant, _, _ = await workspace(client, login, owner_sql)
    owner_sql("UPDATE tenant SET time_zone = 'Africa/Lagos' WHERE id = %s", tenant["id"])
    assert await run(tenant["id"], DUE) == 0
