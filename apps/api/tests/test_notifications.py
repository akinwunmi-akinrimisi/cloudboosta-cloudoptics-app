"""Connection alerts by email, the Postmark sender, health checks and the audit log view."""

import json
import secrets

import httpx
import pytest
from fake_aws import FakeSessionFactory, client_error, healthy_clients
from httpx import AsyncClient

from cloudoptics.connectors import service
from cloudoptics.notifications import alerts
from cloudoptics.notifications.email import Email, EmailError, PostmarkMailer, set_mailer


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[Email] = []
        self.error: EmailError | None = None

    async def send(self, email: Email) -> None:
        if self.error:
            raise self.error
        self.sent.append(email)


@pytest.fixture
def mailer():  # type: ignore[no-untyped-def]
    m = FakeMailer()
    set_mailer(m)
    yield m
    set_mailer(None)


@pytest.fixture
def aws():  # type: ignore[no-untyped-def]
    factory = FakeSessionFactory()
    service.set_session_factory(factory)
    yield factory
    service.set_session_factory(None)


async def connect(
    client: AsyncClient, login, email: str, aws=None
) -> tuple[str, str, dict[str, str]]:  # type: ignore[no-untyped-def]
    account = "".join(secrets.choice("0123456789") for _ in range(12))
    if aws is not None:
        aws.clients = healthy_clients(account)
    headers = await login(email)
    tenant = (
        await client.post("/v1/tenants", json={"name": "W", "countryCode": "KE"}, headers=headers)
    ).json()["id"]
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": account}, headers=headers
    )
    return tenant, r.json()["connection"]["id"], headers


async def validate(
    client: AsyncClient, tenant: str, connection: str, headers: dict[str, str]
) -> str:
    r = await client.post(
        f"/v1/tenants/{tenant}/connections/{connection}/validate", headers=headers
    )
    assert r.status_code == 200, r.text
    return str(r.json()["status"])


def test_alert_only_when_a_working_connection_gets_worse() -> None:
    assert alerts.should_alert("healthy", "failed")
    assert alerts.should_alert("healthy", "degraded")
    assert alerts.should_alert("degraded", "failed")
    assert not alerts.should_alert(None, "failed")  # never worked: still being set up
    assert not alerts.should_alert("failed", "failed")
    assert not alerts.should_alert("failed", "degraded")
    assert not alerts.should_alert("degraded", "healthy")


async def test_broken_connection_emails_admins_once_a_day(
    client: AsyncClient, login, owner_sql, mailer, aws
) -> None:
    tenant, connection, headers = await connect(client, login, "admin-alerts@example.com", aws)
    aws.assume_error = client_error("AccessDenied", "AssumeRole")
    assert await validate(client, tenant, connection, headers) == "failed"
    assert mailer.sent == []  # first check failing: no email, the admin sees it on screen

    aws.assume_error = None
    assert await validate(client, tenant, connection, headers) == "healthy"
    aws.assume_error = client_error("AccessDenied", "AssumeRole")
    assert await validate(client, tenant, connection, headers) == "failed"
    [email] = mailer.sent
    assert email.to == ["admin-alerts@example.com"]
    account = owner_sql("SELECT aws_account_id FROM cloud_connection WHERE id = %s", connection)
    assert (
        email.subject == f"Action needed: your AWS connection {account[0][0]} has stopped working"
    )
    assert "/settings/connections" in email.text and "<a href=" in email.html
    assert email.tag == "connection-alert"

    # Back to healthy and broken again on the same day: no second email.
    aws.assume_error = None
    await validate(client, tenant, connection, headers)
    aws.assume_error = client_error("AccessDenied", "AssumeRole")
    await validate(client, tenant, connection, headers)
    assert len(mailer.sent) == 1
    assert owner_sql(
        "SELECT kind, status, recipients FROM notification WHERE tenant_id = %s", tenant
    ) == [("connection_alert", "sent", ["admin-alerts@example.com"])]


async def test_failed_send_is_recorded(client: AsyncClient, login, owner_sql, mailer, aws) -> None:
    tenant, connection, headers = await connect(client, login, "admin-bounce@example.com", aws)
    await validate(client, tenant, connection, headers)
    mailer.error = EmailError("postmark_422: 400: sender signature not confirmed")
    aws.assume_error = client_error("AccessDenied", "AssumeRole")
    assert await validate(client, tenant, connection, headers) == "failed"
    assert owner_sql("SELECT status, error FROM notification WHERE tenant_id = %s", tenant) == [
        ("failed", "postmark_422: 400: sender signature not confirmed")
    ]


async def test_health_check_job_queues_checks_and_alerts(
    client: AsyncClient, login, owner_sql, monkeypatch, mailer, aws
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.connectors import jobs
    from cloudoptics.notifications import jobs as notification_jobs

    tenant, connection, headers = await connect(client, login, "admin-health@example.com", aws)
    _, never_validated, _ = await connect(client, login, "other@example.com", aws)
    await validate(client, tenant, connection, headers)
    queued: list[tuple[str, dict[str, str]]] = []

    async def fake_defer(app, name, lock, **kwargs):  # type: ignore[no-untyped-def]
        queued.append((name, kwargs))

    monkeypatch.setattr(jobs, "defer_once", fake_defer)
    monkeypatch.setattr(notification_jobs, "defer_once", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    await jobs.health_check(0)
    checked = [kw["connection_id"] for name, kw in queued if name == jobs.CHECK_TASK]
    assert connection in checked and never_validated not in checked

    queued.clear()
    aws.assume_error = client_error("AccessDenied", "AssumeRole")
    assert await jobs.check_connection(tenant, connection) == "failed"
    # A health check queues the alert and nothing else (no loads, no discovery).
    assert queued == [
        (notification_jobs.ALERT_TASK, {"tenant_id": tenant, "connection_id": connection})
    ]
    assert await notification_jobs.connection_alert(tenant, connection) == "sent"
    assert mailer.sent[0].to == ["admin-health@example.com"]
    assert await notification_jobs.connection_alert(tenant, connection) == "skipped"


async def test_postmark_mailer_sends_and_reports_errors() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content)["To"] == "bad@example.com":
            return httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid email request"})
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mailer = PostmarkMailer("token-1", "CloudOptics <alerts@example.com>", client=http)
        await mailer.send(Email(["a@example.com", "b@example.com"], "Hi", "text", "<p>x</p>", "t"))
        body = json.loads(requests[0].content)
        assert requests[0].headers["X-Postmark-Server-Token"] == "token-1"
        assert body["To"] == "a@example.com, b@example.com"
        assert body["From"] == "CloudOptics <alerts@example.com>"
        assert body["MessageStream"] == "outbound"
        with pytest.raises(EmailError, match="postmark_422: 300: Invalid email request"):
            await mailer.send(Email(["bad@example.com"], "Hi", "t", "h", "t"))


async def test_audit_log_view_for_admins(
    client: AsyncClient, login, owner_sql, mailer, aws
) -> None:
    tenant, connection, headers = await connect(client, login, "auditor@example.com", aws)
    await validate(client, tenant, connection, headers)
    base = f"/v1/tenants/{tenant}/audit-log"
    page = (await client.get(base, headers=headers)).json()
    assert page["verified"] is True
    actions = [e["action"] for e in page["entries"]]
    assert actions == ["connection.status_changed", "connection.created", "tenant.created"]
    created = page["entries"][1]
    assert created["actor"] == {"type": "user", "email": "auditor@example.com", "name": "Test User"}
    assert len(created["after"]["awsAccountId"]) == 12

    first = (await client.get(f"{base}?limit=2", headers=headers)).json()
    assert len(first["entries"]) == 2 and first["nextBefore"] == first["entries"][1]["id"]
    rest = (
        await client.get(f"{base}?limit=2&before={first['nextBefore']}", headers=headers)
    ).json()
    assert [e["action"] for e in rest["entries"]] == ["tenant.created"]
    assert rest["nextBefore"] is None

    # Viewers cannot read the audit log.
    viewer = await login("viewer-audit@example.com")
    user_id = (await client.get("/v1/me", headers=viewer)).json()["id"]
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, 'viewer')",
        tenant,
        user_id,
    )
    assert (await client.get(base, headers=viewer)).status_code == 403

    owner_sql(
        "UPDATE audit_log SET after = '{}' WHERE tenant_id = %s AND action = 'tenant.created'",
        tenant,
    )
    assert (await client.get(base, headers=headers)).json()["verified"] is False
