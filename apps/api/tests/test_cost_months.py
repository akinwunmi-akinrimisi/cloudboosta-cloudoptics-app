"""Review previous months: the month list and the cost views for an ended month."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from test_budgets import workspace
from test_trends import bill, days

from cloudoptics.ingestion import queries

AUG, SEP, OCT = date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)


def month_of_bills(owner_sql, tenant: dict[str, Any], connection: str, first: date,  # type: ignore[no-untyped-def]
                   last: date, ec2: float, s3: float = 0.0) -> None:  # fmt: skip
    for day in days(first, last):
        bill(owner_sql, tenant, connection, day, ec2)
        if s3:
            bill(owner_sql, tenant, connection, day, s3, "AmazonS3", "EUW2-TimedStorage", None)


async def get(client: AsyncClient, tenant: dict[str, Any], headers: dict[str, str], path: str,
              **params: str) -> dict[str, Any]:  # fmt: skip
    r = await client.get(f"/v1/tenants/{tenant['id']}/costs/{path}", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_month_state() -> None:
    assert queries.month_state(OCT, date(2026, 10, 31)) == "current"
    assert queries.month_state(OCT, date(2026, 11, 1)) == "changing"
    assert queries.month_state(OCT, date(2026, 11, 5)) == "changing"
    assert queries.month_state(OCT, date(2026, 11, 6)) == "final"
    assert queries.past_month(None, date(2026, 11, 6)) is None
    assert queries.past_month(date(2026, 11, 1), date(2026, 11, 6)) is None
    assert queries.past_month(date(2026, 10, 15), date(2026, 11, 6)) == OCT


async def test_months_lists_bills_and_provisional_months(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    assert (await get(client, tenant, headers, "months", today="2026-11-03"))["months"] == []

    month_of_bills(owner_sql, tenant, connection, AUG, date(2026, 8, 31), 10)
    month_of_bills(owner_sql, tenant, connection, SEP, date(2026, 9, 30), 20)
    month_of_bills(owner_sql, tenant, connection, OCT, date(2026, 10, 31), 30)
    owner_sql(
        "INSERT INTO monthly_report (tenant_id, month, content) VALUES (%s, %s, %s)",
        tenant["id"],
        SEP,
        json.dumps({"spend": {"usd": 600}}),
    )
    # November so far has only Cost Explorer's provisional figures.
    owner_sql(
        "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, period_start, through_date, "
        "effective_cost_usd, billed_cost_usd, credits_usd) VALUES (%s, %s, %s, %s, 55, 55, 0)",
        tenant["id"],
        connection,
        date(2026, 11, 1),
        date(2026, 11, 2),
    )
    body = await get(client, tenant, headers, "months", today="2026-11-03")
    assert body["currency"] == "USD"
    assert body["months"] == [
        {
            "month": "2026-11",
            "source": "cost_explorer",
            "total": 55.0,
            "dataThrough": "2026-11-02",
            "state": "current",
            "report": None,
        },
        {
            "month": "2026-10",
            "source": "cur",
            "total": 930.0,
            "dataThrough": "2026-10-31",
            "state": "changing",
            "report": "draft",
        },
        {
            "month": "2026-09",
            "source": "cur",
            "total": 600.0,
            "dataThrough": "2026-09-30",
            "state": "final",
            "report": "stored",
        },
        {
            "month": "2026-08",
            "source": "cur",
            "total": 310.0,
            "dataThrough": "2026-08-31",
            "state": "final",
            "report": "draft",
        },
    ]
    later = await get(client, tenant, headers, "months", today="2026-11-20")
    assert [m["state"] for m in later["months"]] == ["current", "final", "final", "final"]
    # Months after "today" are not listed.
    earlier = await get(client, tenant, headers, "months", today="2026-09-10")
    assert [m["month"] for m in earlier["months"]] == ["2026-09", "2026-08"]


async def test_a_past_month_shows_the_whole_month(client: AsyncClient, login, owner_sql) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    month_of_bills(owner_sql, tenant, connection, AUG, date(2026, 8, 31), 10, s3=2)
    month_of_bills(owner_sql, tenant, connection, SEP, date(2026, 9, 30), 20, s3=1)
    month_of_bills(owner_sql, tenant, connection, OCT, date(2026, 10, 31), 30)
    month_of_bills(owner_sql, tenant, connection, date(2026, 11, 1), date(2026, 11, 2), 40)
    today = "2026-11-03"

    current = await get(client, tenant, headers, "summary", today=today)
    assert current["month"] == "2026-11"
    assert current["monthState"] == "current"
    assert current["forecast"] is not None

    sep = await get(client, tenant, headers, "summary", today=today, month="2026-09")
    assert sep["month"] == "2026-09"
    assert sep["monthState"] == "final"
    assert sep["status"] == "ready"
    assert sep["dataThrough"] == "2026-09-30"
    assert sep["monthToDate"]["effective"] == pytest.approx(630.0)
    assert sep["previousMonth"] == pytest.approx(372.0)
    assert sep["forecast"] is None
    october = await get(client, tenant, headers, "summary", today=today, month="2026-10")
    assert october["monthState"] == "changing"
    # The current month asked for by name is the default view.
    same = await get(client, tenant, headers, "summary", today=today, month="2026-11")
    assert same == current

    daily = await get(client, tenant, headers, "daily", today=today, month="2026-09")
    assert [p["date"] for p in daily["points"]][:1] == ["2026-09-01"]
    assert len(daily["points"]) == 30
    assert daily["points"][-1] == {"date": "2026-09-30", "amount": 21.0}
    assert (await get(client, tenant, headers, "daily", today=today, month="2026-07"))[
        "points"
    ] == []
    recent = await get(client, tenant, headers, "daily", today=today, days="7")
    assert recent["points"][-1]["date"] == "2026-11-02"

    breakdown = await get(client, tenant, headers, "breakdown", today=today, month="2026-08")
    assert breakdown["month"] == "2026-08"
    assert breakdown["total"] == pytest.approx(372.0)
    assert [i["key"] for i in breakdown["items"]] == ["AmazonEC2", "AmazonS3"]

    # A past month: the whole month against the whole month before.
    changes = await get(client, tenant, headers, "changes", today=today, month="2026-09")
    assert changes["month"] == "2026-09"
    assert changes["current"] == {"from": "2026-09-01", "to": "2026-09-30"}
    assert changes["previous"] == {"from": "2026-08-01", "to": "2026-08-31"}
    assert changes["usd"] == pytest.approx(630.0)
    assert changes["previousUsd"] == pytest.approx(372.0)
    assert changes["rises"][0]["serviceCode"] == "AmazonEC2"
    assert changes["falls"][0]["serviceCode"] == "AmazonS3"
    # The current month keeps its "same days last month" comparison.
    now = await get(client, tenant, headers, "changes", today=today)
    assert now["current"] == {"from": "2026-11-01", "to": "2026-11-02"}
    assert now["previous"] == {"from": "2026-10-01", "to": "2026-10-02"}
    none = await get(client, tenant, headers, "changes", today=today, month="2026-06")
    assert none["status"] == "no_data"


async def test_month_parameter_is_checked(client: AsyncClient, login, owner_sql) -> None:
    tenant, headers, _ = await workspace(client, login, owner_sql)
    base = f"/v1/tenants/{tenant['id']}/costs"
    for path in ("summary", "daily", "breakdown", "changes"):
        r = await client.get(f"{base}/{path}", params={"month": "2026-13"}, headers=headers)
        assert r.status_code == 422, path
        r = await client.get(
            f"{base}/{path}", params={"month": "2026-12", "today": "2026-11-03"}, headers=headers
        )
        assert r.status_code == 422, path
        assert r.json()["title"] == "Choose this month or an earlier one."
    other = await login()
    r = await client.get(f"{base}/months", headers=other)
    assert r.status_code == 404


async def test_a_provisional_past_month_has_no_forecast(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    owner_sql(
        "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, period_start, through_date, "
        "effective_cost_usd, billed_cost_usd, credits_usd) VALUES (%s, %s, %s, %s, 90, 90, 0)",
        tenant["id"],
        connection,
        OCT,
        date(2026, 10, 31),
    )
    body = await get(client, tenant, headers, "summary", today="2026-11-03", month="2026-10")
    assert body["status"] == "provisional"
    assert body["forecast"] is None
    assert body["monthState"] == "changing"
    assert body["monthToDate"]["effective"] == 90.0
