"""Budgets and budget alerts (PRD US-6.1/6.2, WBS API-6)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from httpx import AsyncClient
from test_identity import create_tenant
from test_invites import Outbox, outbox  # noqa: F401 (fixture)

from cloudoptics.budgets import jobs, service
from cloudoptics.platform.db import get_sessionmaker, scope

ACCOUNT, OTHER = "111122223333", "444455556666"


async def workspace(
    client: AsyncClient, login, owner_sql, country: str = "NG"
) -> tuple[dict[str, Any], dict[str, str], str]:
    headers = await login()
    tenant = await create_tenant(client, headers, countryCode=country)
    r = await client.post(
        f"/v1/tenants/{tenant['id']}/connections",
        json={"awsAccountId": ACCOUNT, "accountType": "management"},
        headers=headers,
    )
    connection = r.json()["connection"]["id"]
    for account in (ACCOUNT, OTHER):
        owner_sql(
            "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, "
            "account_type) VALUES (gen_random_uuid(), %s, %s, %s, 'member')",
            tenant["id"],
            connection,
            account,
        )
    return tenant, headers, connection


def spend(
    owner_sql, tenant: dict[str, Any], connection: str, day: date, account: str, usd: float,
    category: str = "Usage",
) -> None:  # fmt: skip
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, charge_category, "
        "billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) VALUES (%s, %s, %s, "
        "%s, %s, 'AmazonEC2', 'Amazon EC2', 'Compute', %s, %s, %s, %s, 'v1')",
        tenant["id"],
        connection,
        day,
        day.replace(day=1),
        account,
        category,
        usd,
        usd,
        usd,
    )


async def test_members_create_budgets_and_viewers_only_read(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    base = f"/v1/tenants/{tenant['id']}/budgets"
    today = datetime.now(UTC).date()
    spend(owner_sql, tenant, connection, today, ACCOUNT, 30)
    spend(owner_sql, tenant, connection, today, OTHER, 10)

    r = await client.post(base, json={"name": "Company", "amount": 100}, headers=admin)
    assert r.status_code == 201, r.text
    budget = r.json()
    assert budget["currency"] == "USD" and budget["scope"] == {"type": "tenant", "accounts": []}
    assert [(t["kind"], t["percent"]) for t in budget["thresholds"]] == [
        ("actual", 50),
        ("actual", 80),
        ("actual", 100),
        ("forecast", 100),
    ]
    assert budget["actualUsd"] == 40 and budget["actualPercent"] == 40
    assert budget["period"] == today.replace(day=1).isoformat()

    # An account-scoped budget counts only its accounts.
    r = await client.post(
        base,
        json={
            "name": "Data team",
            "amount": 20,
            "scope": {"type": "accounts", "accounts": [OTHER]},
        },
        headers=admin,
    )
    assert r.json()["actualUsd"] == 10

    viewer_email = f"viewer-{uuid.uuid4().hex[:6]}@example.com"
    viewer = await login(viewer_email)
    await client.get("/v1/me", headers=viewer)  # the user exists from the first call
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) SELECT %s, id, 'viewer' "
        "FROM app_user WHERE email = %s",
        tenant["id"],
        viewer_email,
    )
    assert len((await client.get(base, headers=viewer)).json()) == 2
    assert (
        await client.post(base, json={"name": "x", "amount": 1}, headers=viewer)
    ).status_code == 403
    assert (await client.delete(f"{base}/{budget['id']}", headers=viewer)).status_code == 403

    r = await client.patch(
        f"{base}/{budget['id']}", json={"amount": 50, "name": " Total "}, headers=admin
    )
    assert (r.json()["amount"], r.json()["name"], r.json()["actualPercent"]) == (50, "Total", 80)
    assert (await client.delete(f"{base}/{budget['id']}", headers=admin)).status_code == 204
    assert (await client.get(f"{base}/{budget['id']}", headers=admin)).status_code == 404
    actions = [
        a for (a,) in owner_sql(
            "SELECT action FROM audit_log WHERE tenant_id = %s AND action LIKE 'budget.%%' "
            "ORDER BY id",
            tenant["id"],
        )
    ]  # fmt: skip
    assert actions == ["budget.created", "budget.created", "budget.updated", "budget.deleted"]


async def test_budgets_check_currency_accounts_and_recipients(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, _ = await workspace(client, login, owner_sql)
    base = f"/v1/tenants/{tenant['id']}/budgets"

    async def status_of(body: dict[str, Any]) -> int:
        return (
            await client.post(base, json={"name": "B", "amount": 10, **body}, headers=admin)
        ).status_code

    assert await status_of({"currency": "EUR"}) == 422  # not the workspace currency
    assert await status_of({"currency": "NGN"}) == 201
    assert await status_of({"scope": {"type": "accounts", "accounts": ["999999999999"]}}) == 422
    assert await status_of({"scope": {"type": "accounts", "accounts": []}}) == 422
    assert await status_of({"recipients": {"users": [str(uuid.uuid4())]}}) == 422
    assert await status_of({"recipients": {"emails": ["not-an-email"]}}) == 422
    twice = [{"kind": "actual", "percent": 80}, {"kind": "actual", "percent": 80}]
    assert await status_of({"thresholds": twice}) == 422
    assert await status_of({"amount": 0}) == 422
    other, other_admin, _ = await workspace(client, login, owner_sql)
    budget = (await client.get(base, headers=admin)).json()[0]
    # Budgets stay inside their workspace.
    r = await client.get(f"/v1/tenants/{other['id']}/budgets/{budget['id']}", headers=other_admin)
    assert r.status_code == 404


async def run(tenant_id: str, today: date) -> int:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await service.alert_budgets(session, tid, today)


async def test_thresholds_alert_once_a_month_in_the_budget_currency(
    client: AsyncClient,
    login,
    owner_sql,
    outbox: Outbox,  # noqa: F811
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql, country="UG")
    me = (await client.get("/v1/me", headers=admin)).json()
    day = date(2026, 9, 10)
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
        "VALUES (gen_random_uuid(), 'UGX', %s, 'official', 4000, 'manual', %s)",
        day,
        me["id"],
    )
    # 10 days at USD 10 a day, plus tax that the run rate leaves out.
    for d in range(1, 11):
        spend(owner_sql, tenant, connection, date(2026, 9, d), ACCOUNT, 10)
    spend(owner_sql, tenant, connection, day, ACCOUNT, 20, category="Tax")
    owner_sql(
        "INSERT INTO budget (id, tenant_id, name, scope, amount, currency, thresholds, "
        "recipients) VALUES (gen_random_uuid(), %s, 'Kampala', '{\"type\": \"tenant\"}', "
        "1000000, 'UGX', %s, %s)",
        tenant["id"],
        '[{"kind": "actual", "percent": 50}, {"kind": "actual", "percent": 100}, '
        '{"kind": "forecast", "percent": 100}]',
        '{"users": ["' + me["id"] + '"], "emails": ["finance@example.com"]}',
    )
    # UGX 1,000,000 is USD 250. Spent USD 120 (48%); forecast 120 + 10 x 20 days = 320.
    tid = tenant["id"]
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        row = (await session.execute(service_select())).one()
        ev = await service.evaluate(session, row, day)
    assert (ev.amount_usd, ev.actual_usd, ev.forecast_usd, ev.status) == (250, 120, 320, "at_risk")
    assert ev.in_budget_currency(ev.actual_usd) == 480_000
    assert [(t.kind, t.percent) for t in ev.thresholds if t.crossed] == [("forecast", 100)]

    assert await run(tid, day) == 1
    [mail] = outbox.sent
    assert sorted(mail.to) == sorted(["finance@example.com", me["email"].lower()])
    assert "forecast to reach 100%" in mail.subject and "UGX 480,000.00" in mail.text
    assert await run(tid, day) == 0  # once a month

    # More spend: 50% of actual crosses, with its own email.
    spend(owner_sql, tenant, connection, day, OTHER, 20)
    assert await run(tid, day) == 1
    assert "50% of UGX 1,000,000.00 spent" in outbox.sent[-1].subject
    # A new month starts again (no data there yet, so nothing crosses).
    assert await run(tid, date(2026, 10, 1)) == 0


async def test_failed_email_is_tried_again_and_no_rate_means_no_alert(
    client: AsyncClient, login, owner_sql
) -> None:
    from cloudoptics.notifications.email import set_mailer

    tenant, admin, connection = await workspace(client, login, owner_sql, country="RW")
    day = date(2026, 9, 5)
    spend(owner_sql, tenant, connection, day, ACCOUNT, 100)
    base = f"/v1/tenants/{tenant['id']}/budgets"
    r = await client.post(
        base,
        json={"name": "RWF", "amount": 1000, "currency": "RWF",
              "recipients": {"emails": ["a@example.com"]}},
        headers=admin,
    )  # fmt: skip
    assert r.json()["status"] == "no_rate" and r.json()["amountUsd"] is None
    assert await run(tenant["id"], day) == 0

    await client.post(
        base,
        json={"name": "USD", "amount": 50, "recipients": {"emails": ["a@example.com"]}},
        headers=admin,
    )
    failing = Outbox(fail=True)
    set_mailer(failing)
    try:
        assert await run(tenant["id"], day) == 0
        statuses = owner_sql(
            "SELECT DISTINCT status FROM notification WHERE tenant_id = %s", tenant["id"]
        )
        assert statuses == [("failed",)]
        working = Outbox()
        set_mailer(working)
        assert await run(tenant["id"], day) == 1  # tried again
        assert await run(tenant["id"], day) == 0
    finally:
        set_mailer(None)


async def test_new_bills_and_the_hourly_poll_queue_an_evaluation(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs

    tenant, _, _ = await workspace(client, login, owner_sql)
    queued: list[str] = []

    async def fake_defer(app, tenant_id):  # type: ignore[no-untyped-def]
        queued.append(tenant_id)

    monkeypatch.setattr(jobs, "defer_evaluate", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.poll(0) == len(queued)
    assert tenant["id"] in queued
    assert await jobs.evaluate(tenant["id"]) == 0  # no budgets yet


def service_select() -> Any:
    from sqlalchemy import text

    return text(
        "SELECT id, name, scope, amount, currency, thresholds, recipients FROM budget LIMIT 1"
    )
