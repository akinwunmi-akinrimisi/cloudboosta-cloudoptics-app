"""Forecasts with an FX scenario (PRD US-3.2, FR-ANA-09) and what changed (FR-ANA-12)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from test_budgets import ACCOUNT, workspace
from test_digest import member

from cloudoptics.ingestion import queries
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.reporting import digest, monthly
from cloudoptics.trends import changes, forecast

NAMES = {
    "AmazonEC2": "Amazon EC2",
    "AmazonS3": "Amazon S3",
    "AWSSupportBusiness": "AWS Support (Business)",
}


def bill(
    owner_sql, tenant: dict[str, Any], connection: str, day: date, usd: float,
    service: str = "AmazonEC2", usage_type: str | None = "EUW2-BoxUsage:t3.micro",
    region: str | None = "eu-west-2", category: str = "Usage",
) -> None:  # fmt: skip
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, region_id, usage_type, "
        "charge_category, billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Compute', %s, %s, %s, %s, %s, %s, 'v1')",
        tenant["id"],
        connection,
        day,
        day.replace(day=1),
        ACCOUNT,
        service,
        NAMES[service],
        region,
        usage_type,
        category,
        usd,
        usd,
        usd,
    )


def support(owner_sql, tenant, connection, day: date) -> None:  # type: ignore[no-untyped-def]
    bill(owner_sql, tenant, connection, day, 100, "AWSSupportBusiness", "Business", None)


def days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


# --- the maths --------------------------------------------------------------------------


def test_a_straight_line_is_found_and_bounded() -> None:
    line = forecast.fit([10 + 0.5 * t for t in range(60)], run_rate=0)
    assert line.method == "trend"
    assert (round(line.intercept, 6), round(line.slope, 6), round(line.sigma, 6)) == (10, 0.5, 0)
    # The last 30 days average 32.25, so no day goes past 1.5 x that, however far out.
    assert line.daily(60) == 40
    assert line.daily(1000) == pytest.approx(48.375)
    falling = forecast.fit([100 - t for t in range(60)], run_rate=0)
    assert falling.daily(1000) == pytest.approx(0.5 * falling.level)

    short = forecast.fit([10, 12, 14], run_rate=13)
    assert (short.method, short.daily(500), short.slope) == ("run_rate", 13, 0)
    assert forecast.fit([], run_rate=5).daily(0) == 5


def test_the_range_widens_with_each_month_and_stays_within_limits() -> None:
    assert forecast.spread(1000, 0, 30, 1) == 50  # at least 5% a month ahead
    assert forecast.spread(1000, 0, 30, 3) == 150
    assert forecast.spread(1000, 100, 30, 3) == 500  # at most 50%
    assert forecast.spread(1000, 8, 30, 1) == round(1.645 * 8 * 30**0.5, 2)
    assert forecast.spread(1000, 5, 9, 0) == round(1.645 * 5 * 3, 2)  # this month: noise only
    assert forecast.spread(1000, 5, 0, 0) == 0


def test_same_days_last_month_stop_at_its_end() -> None:
    assert changes.periods(date(2026, 3, 31)) == (
        (date(2026, 3, 1), date(2026, 3, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
    )
    assert changes.periods(date(2026, 3, 10))[1] == (date(2026, 2, 1), date(2026, 2, 10))


def test_contributors_and_their_sentences() -> None:
    def row(cur: bool, svc: str, region: str | None, ut: str | None, usd: float):  # type: ignore[no-untyped-def]
        return queries.ChangeRow(cur, svc, NAMES[svc], region, ut, usd)

    found = changes.contributors(
        [
            row(True, "AmazonEC2", "eu-west-1", "BoxUsage:m5.large", 300),
            row(False, "AmazonEC2", "eu-west-1", "BoxUsage:m5.large", 100),
            row(True, "AmazonEC2", "eu-west-1", "EBS:VolumeUsage", 40),
            row(False, "AmazonEC2", "eu-west-1", "EBS:VolumeUsage", 50),
            row(True, "AmazonS3", None, "TimedStorage", 50),
            row(False, "AmazonS3", None, "TimedStorage", 80),
            row(True, "AmazonS3", "eu-west-1", None, 25),
            row(True, "AmazonEC2", "us-east-1", "BoxUsage", 10.5),
            row(False, "AmazonEC2", "us-east-1", "BoxUsage", 10),  # under USD 1: left out
        ]
    )
    usd = lambda v: f"USD {v:,.2f}"  # noqa: E731
    assert [changes.sentence(c, usd) for c in found] == [
        "Amazon EC2 in eu-west-1 rose USD 190.00 (+127%) mainly due to BoxUsage:m5.large.",
        "Amazon S3 fell USD 30.00 (-38%) mainly due to TimedStorage.",
        "Amazon S3 in eu-west-1 rose USD 25.00 (new this month).",
    ]
    assert found[0].usage_type_change_usd == 200
    w = changes.WhatChanged(
        date(2026, 3, 1), (date(2026, 3, 1),) * 2, (date(2026, 2, 1),) * 2, 0, 0,
        rises=[found[0], found[2]], falls=[found[1]],
    )  # fmt: skip
    assert changes.headline(w, usd) == changes.sentence(found[0], usd)
    assert changes.headline(None, usd) is None


# --- through the API ----------------------------------------------------------------------


async def test_forecast_with_monthly_charges_trend_rate_and_fx_split(
    client: AsyncClient, login, owner_sql
) -> None:
    # Britain: no other test uses GBP rates.
    tenant, admin, connection = await workspace(client, login, owner_sql, country="GB")
    tid = tenant["id"]
    url = f"/v1/tenants/{tid}/costs/forecast"
    empty = (await client.get(url, headers=admin)).json()
    assert (empty["status"], empty["monthEnd"], empty["months"]) == ("no_data", None, [])

    # EC2 at USD 10 a day from July, 17 a day from 8 October; AWS Support USD 100 on
    # 1 August and 1 September; some tax in September.
    for day in days(date(2026, 7, 1), date(2026, 10, 14)):
        bill(owner_sql, tenant, connection, day, 17 if day >= date(2026, 10, 8) else 10)
    support(owner_sql, tenant, connection, date(2026, 8, 1))
    support(owner_sql, tenant, connection, date(2026, 9, 1))
    me = (await client.get("/v1/me", headers=admin)).json()
    for day in days(date(2026, 8, 1), date(2026, 9, 30)):
        owner_sql(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
            "VALUES (gen_random_uuid(), 'GBP', %s, 'official', %s, 'manual', %s)",
            day,
            0.8 if day.month == 8 else 0.75,
            me["id"],
        )
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, source_note, "
        "entered_by) VALUES (gen_random_uuid(), 'GBP', '2026-10-12', 'official', 0.7, "
        "'manual', 'Bank of England', %s)",
        me["id"],
    )

    o = (await client.get(url, params={"today": "2026-10-15"}, headers=admin)).json()
    assert (o["status"], o["currency"], o["dataThrough"]) == ("ready", "GBP", "2026-10-14")
    # Today's rate: set on the 12th, carried forward so flagged estimated.
    assert (o["quote"]["rate"], o["quote"]["rateDate"], o["quote"]["estimated"]) == (
        0.7, "2026-10-12", True,
    )  # fmt: skip
    # 7 x 10 + 7 x 17 = 189 so far; 17 days left at the last 7 days' 17 a day; Support's
    # 100 is not on October's bills yet.
    me_ = o["monthEnd"]
    assert me_["month"] == "2026-10"
    f = me_["forecast"]
    assert (f["amount"], f["runRateDays"], f["dailyRunRate"], f["monthlyCharges"]) == (
        578, 7, 17, 100,
    )  # fmt: skip
    assert me_["low"] < 578 < me_["high"]
    summary = (await client.get(f"/v1/tenants/{tid}/costs/summary?today=2026-10-15",
                                headers=admin)).json()  # fmt: skip
    assert summary["forecast"] == f

    # Next three months: the trend through the last 90 days (Support left out of the line
    # and added back), rising a little, within its range.
    assert [m["month"] for m in o["months"]] == ["2026-11", "2026-12", "2027-01"]
    assert (o["monthlyChargesUsd"], o["taxShare"], o["trendDays"]) == (100, 0, 90)
    for m in o["months"]:
        assert m["method"] == "trend" and m["low"] < m["amount"] < m["high"]
        assert 30 * 10 + 100 < m["amount"] < 31 * 17 * 1.5 + 100
    widths = [m["high"] - m["low"] for m in o["months"]]
    assert widths == sorted(widths)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        direct = await forecast.outlook(session, uuid.UUID(tid), date(2026, 10, 15))
    assert direct.decomposition is not None and direct.decomposition.fx_effect == -20
    assert [m.amount for m in direct.months] == [m["amount"] for m in o["months"]]

    # September against August: usage (400 - 410 USD) at August's 0.8 and the rate move
    # (0.75 - 0.8) on September's 400.
    d = o["decomposition"]
    assert (d["month"], d["previousMonth"], d["usd"], d["previousUsd"]) == (
        "2026-09", "2026-08", 400, 410,
    )  # fmt: skip
    assert (d["local"], d["previousLocal"], d["rate"], d["previousRate"]) == (300, 328, 0.75, 0.8)
    assert (d["usageEffect"], d["fxEffect"]) == (-8, -20)

    # Once Support lands on 1 October it is in the month so far, not projected again.
    support(owner_sql, tenant, connection, date(2026, 10, 1))
    f = (await client.get(url, params={"today": "2026-10-15"}, headers=admin)).json()
    assert (f["monthEnd"]["forecast"]["amount"], f["monthEnd"]["forecast"]["monthlyCharges"]) == (
        578, 0,
    )  # fmt: skip
    # Early in the month, the 1st's Support charge is inside the 7-day window but does not
    # count towards the daily rate: 130 so far + 28 days x 10.
    early = (await client.get(url, params={"today": "2026-10-03"}, headers=admin)).json()
    f = early["monthEnd"]["forecast"]
    assert (f["amount"], f["dailyRunRate"], f["daysCovered"]) == (410, 10, 3)

    # Budgets use the same forecast.
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        spend = await queries.month_spend(session, date(2026, 10, 15), [ACCOUNT])
    assert (spend.effective, spend.forecast) == (289, 578)

    _, viewer = await member(client, login, owner_sql, tenant, "viewer")
    assert (await client.get(url, headers=viewer)).status_code == 200
    stranger = await login()
    assert (await client.get(url, headers=stranger)).status_code in (403, 404)


async def test_a_short_history_carries_the_run_rate_on(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql, country="GB")
    for day in days(date(2026, 10, 1), date(2026, 10, 10)):
        bill(owner_sql, tenant, connection, day, 20)
    bill(owner_sql, tenant, connection, date(2026, 10, 10), 10, category="Tax")
    o = (
        await client.get(
            f"/v1/tenants/{tenant['id']}/costs/forecast",
            params={"today": "2026-10-10"},
            headers=admin,
        )
    ).json()
    # 210 so far (with tax) + 21 days x 20.
    assert o["monthEnd"]["forecast"]["amount"] == 630
    assert o["monthEnd"]["low"] == o["monthEnd"]["high"] == 630  # no noise to go on
    assert [m["method"] for m in o["months"]] == ["run_rate"] * 3
    # November: 30 x 20, plus the window's tax share (10 on 200).
    assert o["months"][0]["amount"] == 630
    assert (o["months"][0]["low"], o["months"][0]["high"]) == (598.5, 661.5)
    assert o["decomposition"] is None  # no earlier months

    # The same, called directly (as a job would).
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tenant["id"]))
        direct = await forecast.outlook(session, uuid.UUID(tenant["id"]), date(2026, 10, 10))
        before = await forecast.outlook(session, uuid.UUID(tenant["id"]), date(2026, 9, 30))
    assert direct.month_end is not None and direct.month_end.amount == 630
    assert direct.month_end_range == (630, 630)
    assert [m.amount for m in direct.months] == [630, 651, 651]  # 30, 31, 31 days
    assert (before.status, before.month_end, before.months) == ("no_data", None, [])


async def test_what_changed_on_the_api_in_the_digest_and_the_report(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql, country="GB")
    tid = tenant["id"]
    url = f"/v1/tenants/{tid}/costs/changes"
    assert (await client.get(url, headers=admin)).json()["status"] == "no_data"
    # September: EC2 10 a day, S3 5 a day. October to the 14th: EC2 a bigger instance
    # (25 a day), S3 2 a day, and a new Lambda bill.
    for day in days(date(2026, 9, 1), date(2026, 9, 30)):
        bill(owner_sql, tenant, connection, day, 10)
        bill(owner_sql, tenant, connection, day, 5, "AmazonS3", "TimedStorage", "eu-west-1")
    for day in days(date(2026, 10, 1), date(2026, 10, 14)):
        bill(owner_sql, tenant, connection, day, 10)
        bill(owner_sql, tenant, connection, day, 15, usage_type="EUW2-BoxUsage:m5.large")
        bill(owner_sql, tenant, connection, day, 2, "AmazonS3", "TimedStorage", "eu-west-1")
        bill(owner_sql, tenant, connection, day, -50, category="Credit")  # left out
    w = (await client.get(url, params={"today": "2026-10-20"}, headers=admin)).json()
    assert w["status"] == "ready" and w["month"] == "2026-10"
    assert w["current"] == {"from": "2026-10-01", "to": "2026-10-14"}
    assert w["previous"] == {"from": "2026-09-01", "to": "2026-09-14"}
    assert (w["usd"], w["previousUsd"], w["changeUsd"]) == (378, 210, 168)
    [rise] = w["rises"]
    assert rise["sentence"] == (
        "Amazon EC2 in eu-west-2 rose USD 210.00 (+150%) mainly due to EUW2-BoxUsage:m5.large."
    )
    assert (rise["usd"], rise["previousUsd"], rise["changePercent"]) == (350, 140, 150)
    [fall] = w["falls"]
    assert fall["sentence"] == (
        "Amazon S3 in eu-west-1 fell USD 42.00 (-60%) mainly due to TimedStorage."
    )
    one = (await client.get(url, params={"today": "2026-10-20", "limit": 1},
                            headers=admin)).json()  # fmt: skip
    assert len(one["rises"]) == len(one["falls"]) == 1

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        built = await digest.build(session, uuid.UUID(tid), date(2026, 10, 5))
        report = await monthly.build(session, uuid.UUID(tid), date(2026, 10, 1))
    assert built is not None and report is not None
    _, text_, html_ = digest.render(built, "https://app.example")
    # As of Sunday 11 October: 11 days at 15 more.
    assert "This month so far: Amazon EC2 in eu-west-2 rose USD 165.00 (+150%)" in text_
    assert "mainly due to EUW2-BoxUsage:m5.large." in html_
    assert report.what_changed == (
        "Amazon EC2 in eu-west-2 rose USD 210.00 (+150%) mainly due to EUW2-BoxUsage:m5.large."
    )
    _, text_, _ = monthly.render(report, "https://app.example")
    assert f"- {report.what_changed}" in text_
