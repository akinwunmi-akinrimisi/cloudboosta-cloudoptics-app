"""Weekly digest and notification preferences (PRD US-6.4, FR-NOT-03, WBS ADM-8)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from test_anomalies import cost
from test_budgets import workspace
from test_invites import Outbox, outbox  # noqa: F401 (fixture)

from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.reporting import digest, jobs

LAGOS = digest.zone("Africa/Lagos")  # UTC+1
WEEK = date(2026, 10, 19)  # a Monday
MONDAY = datetime(2026, 10, 26, 6, 30, tzinfo=UTC)  # 07:30 in Lagos, the next Monday


def test_the_digest_is_due_on_monday_from_seven_local() -> None:
    assert digest.due(datetime(2026, 10, 26, 5, 59, tzinfo=UTC), LAGOS) is None  # 06:59
    assert digest.due(MONDAY, LAGOS) == WEEK
    assert digest.due(datetime(2026, 10, 26, 22, 59, tzinfo=UTC), LAGOS) == WEEK
    assert digest.due(datetime(2026, 10, 27, 7, 0, tzinfo=UTC), LAGOS) is None  # Tuesday
    assert digest.week_before(date(2026, 10, 28)) == WEEK
    assert digest.zone("Not/AZone").key == "UTC"


async def member(client: AsyncClient, login, owner_sql, tenant: dict[str, Any], role: str):  # type: ignore[no-untyped-def]
    email = f"{role}-{uuid.uuid4().hex[:6]}@example.com"
    headers = await login(email)
    await client.get("/v1/me", headers=headers)
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) SELECT %s, id, %s "
        "FROM app_user WHERE email = %s",
        tenant["id"],
        role,
        email,
    )
    return email, headers


def recommendation(
    owner_sql, tenant: dict[str, Any], title: str, savings: float, detected: datetime,
    status: str = "new",
) -> str:  # fmt: skip
    rec_id = str(uuid.uuid4())
    owner_sql(
        "INSERT INTO recommendation (id, tenant_id, fingerprint, type, rule_id, rule_version, "
        "rule_config, generation_id, account_id, region_id, resource_id, title, "
        "cost_now_usd_month, cost_after_usd_month, savings_basis, effort, reversibility, "
        "risk_score, risk_level, confidence, priority_score, engine_version, "
        "first_detected_at, status) VALUES (%s, %s, %s, 'delete_unattached_volume', "
        "'R1_unattached_ebs', 1, '{}', 1, '111122223333', 'eu-west-2', %s, %s, %s, 0, "
        "'list_price', 'S', 'with_snapshot', 10, 'low', 'high', %s, 'test', %s, %s)",
        rec_id,
        tenant["id"],
        rec_id,
        f"vol-{rec_id[:8]}",
        title,
        savings,
        savings,
        detected,
        status,
    )
    return rec_id


async def run(tenant_id: str, now: datetime) -> int:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await digest.send(session, tid, now)


async def test_each_member_gets_one_digest_about_last_week(
    client: AsyncClient,
    login,
    owner_sql,
    outbox: Outbox,  # noqa: F811
) -> None:
    # Egypt: the exchange rate table is shared by every test, so a currency no other uses.
    tenant, admin, connection = await workspace(client, login, owner_sql, country="EG")
    tid = tenant["id"]
    owner_sql("UPDATE tenant SET time_zone = 'Africa/Lagos' WHERE id = %s", tid)
    assert await run(tid, MONDAY) == 0  # no bills: no digest
    me = (await client.get("/v1/me", headers=admin)).json()
    # The week before: USD 10 a day of EC2. Last week: 20 a day, plus S3 and some tax.
    for i in range(7):
        cost(owner_sql, tenant, connection, WEEK - timedelta(days=7 - i), 10)
        cost(owner_sql, tenant, connection, WEEK + timedelta(days=i), 20)
    cost(owner_sql, tenant, connection, WEEK, 7, service_code="AmazonS3", usage_type="Storage")
    cost(owner_sql, tenant, connection, WEEK, 30, category="Tax")
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
        "VALUES (gen_random_uuid(), 'EGP', %s, 'official', 1500, 'manual', %s)",
        WEEK + timedelta(days=6),  # the rate for the week's last day
        me["id"],
    )
    found = datetime(2026, 10, 21, 9, tzinfo=UTC)
    recommendation(owner_sql, tenant, "Delete volume build-cache", 40, found)
    recommendation(owner_sql, tenant, "Release Elastic IP", 3.6, found - timedelta(days=30))
    done = recommendation(owner_sql, tenant, "Old snapshot", 5, found, status="done")
    owner_sql(
        "INSERT INTO recommendation_event (id, tenant_id, recommendation_id, kind, from_status, "
        "to_status, created_at) VALUES (gen_random_uuid(), %s, %s, 'status_changed', 'new', "
        "'done', %s)",
        tid,
        done,
        found,
    )
    owner_sql(
        "INSERT INTO anomaly (id, tenant_id, usage_date, account_id, service_code, "
        "service_name, actual_usd, expected_usd, mad_usd, impact_usd, engine_version) "
        "VALUES (gen_random_uuid(), %s, %s, '111122223333', 'AmazonEC2', 'Amazon EC2', 90, 10, "
        "0, 80, 'test')",
        tid,
        WEEK + timedelta(days=2),
    )
    r = await client.post(
        f"/v1/tenants/{tid}/budgets", json={"name": "Company", "amount": 1000}, headers=admin
    )
    assert r.status_code == 201

    viewer_email, viewer = await member(client, login, owner_sql, tenant, "viewer")
    quiet_email, quiet = await member(client, login, owner_sql, tenant, "member")
    prefs = f"/v1/tenants/{tid}/notification-preferences"
    r = await client.put(f"{prefs}/me", json={"weeklyDigest": False}, headers=quiet)
    assert r.json()["mine"] == {"weeklyDigest": False, "anomalyAlert": True}

    assert await run(tid, MONDAY - timedelta(hours=1)) == 0  # 06:30 in Lagos: not yet
    assert await run(tid, MONDAY) == 2
    assert sorted(m.to[0] for m in outbox.sent) == sorted([me["email"].lower(), viewer_email])
    assert all(len(m.to) == 1 for m in outbox.sent)
    mail = outbox.sent[0]
    # 140 of EC2 and 7 of S3 against 70; tax is left out.
    assert mail.subject == (
        "Your AWS week: USD 147.00 (EGP 220,500) spent (+110% on the week before)"
    )
    text_ = mail.text
    assert "Spent USD 147.00 (EGP 220,500), against USD 70.00 (EGP 105,000)" in text_
    assert "- Amazon EC2: +USD 70.00 (EGP 105,000) (now USD 140.00" in text_
    assert "2 open, worth USD 43.60 (EGP 65,400) a month; 1 new this week." in text_
    assert "- Delete volume build-cache: USD 40.00 (EGP 60,000) a month, low risk" in text_
    assert "(new)" in text_ and "Marked done this week: 1, saving USD 5.00" in text_
    assert "Company (USD 1,000): on track" in text_
    assert "1 unusual day, USD 80.00 (EGP 120,000) above normal." in text_
    assert "Turn it off under Settings" in text_ and "/settings" in text_
    assert "<h3>Recommendations</h3>" in mail.html

    assert await run(tid, MONDAY + timedelta(hours=1)) == 0  # once a week
    # The admin turns the digest off for everyone who has not chosen.
    r = await client.put(f"{prefs}/defaults", json={"weeklyDigest": False}, headers=viewer)
    assert r.status_code == 403
    r = await client.put(f"{prefs}/defaults", json={"weeklyDigest": False}, headers=admin)
    assert r.json()["defaults"] == {"weeklyDigest": False, "anomalyAlert": True}
    assert r.json()["mine"]["weeklyDigest"] is False
    await client.put(f"{prefs}/me", json={"weeklyDigest": True}, headers=quiet)
    next_monday = MONDAY + timedelta(days=7)
    for i in range(7):
        cost(owner_sql, tenant, connection, WEEK + timedelta(days=7 + i), 20)
    assert await run(tid, next_monday) == 1
    assert outbox.sent[-1].to == [quiet_email]
    actions = owner_sql(
        "SELECT action, count(*) FROM audit_log WHERE tenant_id = %s "
        "AND action LIKE 'notification_%%' GROUP BY action ORDER BY action",
        tid,
    )
    assert actions == [
        ("notification_defaults.updated", 1),
        ("notification_preferences.updated", 2),
    ]

    # The preview shows the last full week as emailed.
    preview = f"/v1/tenants/{tid}/digest/preview"
    r = await client.get(preview, params={"week": WEEK.isoformat()}, headers=viewer)
    assert r.json()["subject"] == mail.subject and r.json()["weekEnd"] == "2026-10-25"
    r = await client.get(preview, params={"week": "2026-10-20"}, headers=viewer)
    assert r.status_code == 422


async def test_failed_digest_is_retried_and_anomaly_emails_follow_preferences(
    client: AsyncClient, login, owner_sql
) -> None:
    from cloudoptics.anomalies import service as anomalies
    from cloudoptics.notifications.email import set_mailer

    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    r = await client.get(f"/v1/tenants/{tid}/digest/preview", headers=admin)
    assert r.status_code == 404  # no bills
    cost(owner_sql, tenant, connection, WEEK + timedelta(days=3), 12)
    set_mailer(Outbox(fail=True))
    try:
        assert await run(tid, MONDAY) == 0
        working = Outbox()
        set_mailer(working)
        assert await run(tid, MONDAY) == 1
        assert await run(tid, MONDAY) == 0
    finally:
        set_mailer(None)

    prefs = f"/v1/tenants/{tid}/notification-preferences"
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        settings = anomalies.AlertSettings()
        assert len(await anomalies.recipients(session, uuid.UUID(tid), settings)) == 1
    await client.put(f"{prefs}/me", json={"anomalyAlert": False}, headers=admin)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        assert await anomalies.recipients(session, uuid.UUID(tid), settings) == []
    assert (await client.get(prefs, headers=admin)).json()["mine"]["anomalyAlert"] is False


async def test_the_hourly_poll_queues_every_connected_workspace(
    client: AsyncClient, login, owner_sql, monkeypatch
) -> None:
    import cloudoptics.platform.jobs as platform_jobs

    tenant, _, _ = await workspace(client, login, owner_sql)
    queued: list[str] = []

    async def fake_defer(app, name, lock, **kwargs):  # type: ignore[no-untyped-def]
        queued.append(kwargs["tenant_id"])

    monkeypatch.setattr(jobs, "defer_once", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.poll(0) == len(queued)
    assert tenant["id"] in queued
    assert await jobs.weekly(tenant["id"]) == 0  # not due now, or no bills
