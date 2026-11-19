"""Staff break-glass access (WBS ADM-6, PRD FR-SEC-07, docs/10 T11, docs/11b §7.4)."""

from __future__ import annotations

import dataclasses
import re
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_data_deletion import Outbox
from test_identity import create_tenant

from cloudoptics.identity.auth import DevVerifier
from cloudoptics.notifications.email import set_mailer
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.settings import get_settings
from cloudoptics.support import service

BASE = "/v1/admin/support-access"
JUSTIFICATION = "Customer reports the cost explorer shows no data for October"


@pytest.fixture
def staff_email(app) -> Iterator[str]:  # type: ignore[no-untyped-def]
    email = f"support-{secrets.token_hex(3)}@cloudoptics.example"
    settings = get_settings()
    settings.platform_admin_emails = f"rates-desk@cloudoptics.example, {email.upper()}"
    yield email
    settings.platform_admin_emails = ""


@pytest.fixture
def outbox() -> Iterator[Outbox]:
    box = Outbox()
    set_mailer(box)
    yield box
    set_mailer(None)


async def workspace(client: AsyncClient, login) -> tuple[str, dict[str, str], str]:  # type: ignore[no-untyped-def]
    admin_email = f"admin-{secrets.token_hex(3)}@example.com"
    headers = await login(admin_email)
    tenant = await create_tenant(client, headers, name="Lagos Ledger")
    return tenant["id"], headers, admin_email


async def grant(
    client: AsyncClient, staff: dict[str, str], tenant: str, **body: Any
) -> dict[str, Any]:
    payload = {"tenantId": tenant, "justification": JUSTIFICATION, "ticketRef": "SUP-1", **body}
    r = await client.post(BASE, json=payload, headers=staff)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def audit_entries(client: AsyncClient, tenant: str, headers: dict[str, str]) -> Any:
    r = await client.get(f"/v1/tenants/{tenant}/audit-log", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    return r.json()["entries"]


async def test_grant_lifecycle_request_use_and_end(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, admin, admin_email = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    # No access before a grant: the same 404 as any outsider.
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 404

    g = await grant(client, staff, tenant, ticketRef=" SUP-1042 ")
    assert g["status"] == "active" and g["tenantName"] == "Lagos Ledger"
    assert g["ticketRef"] == "SUP-1042" and g["staffEmail"] == staff_email
    minutes = owner_sql(
        "SELECT extract(epoch FROM expires_at - created_at) / 60 FROM support_access_grant "
        "WHERE id = %s",
        g["id"],
    )[0][0]
    assert round(float(minutes)) == 60  # type: ignore[arg-type]

    # Immediate access, with an email to the workspace's admins.
    [email] = outbox.sent
    assert email.to == [admin_email]
    assert email.subject == "Cloudoptics support accessed your workspace Lagos Ledger"
    assert JUSTIFICATION in email.text and "SUP-1042" in email.text
    assert "read-only" in email.text
    logged = owner_sql(
        "SELECT kind, status FROM notification WHERE tenant_id = %s AND kind = 'support_access'",
        tenant,
    )
    assert logged == [("support_access", "sent")]

    me = (await client.get("/v1/me", headers=staff)).json()
    assert me["staff"] is True
    [m] = me["memberships"]
    assert m["tenantId"] == tenant and m["role"] == "viewer"
    assert m["support"]["grantId"] == g["id"]
    admin_me = (await client.get("/v1/me", headers=admin)).json()
    assert admin_me["staff"] is False and admin_me["memberships"][0]["support"] is None

    r = await client.get(f"/v1/tenants/{tenant}", headers=staff)
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    assert (await client.get(f"/v1/tenants/{tenant}/members", headers=staff)).status_code == 200
    assert (
        await client.get(f"/v1/tenants/{tenant}/costs/summary", headers=staff)
    ).status_code == 200
    requests = owner_sql(
        "SELECT method, path, allowed FROM support_access_request WHERE grant_id = %s ORDER BY id",
        g["id"],
    )
    assert requests == [
        ("GET", f"/v1/tenants/{tenant}", True),
        ("GET", f"/v1/tenants/{tenant}/members", True),
        ("GET", f"/v1/tenants/{tenant}/costs/summary", True),
    ]

    # The tenant's own audit log shows who, why, when and for how long.
    [entry] = [
        e for e in await audit_entries(client, tenant, admin) if e["action"].startswith("support")
    ]
    assert entry["action"] == "support_access.granted"
    assert entry["actor"] == {"type": "staff", "email": staff_email, "name": "Test User"}
    assert entry["after"]["justification"] == JUSTIFICATION
    assert entry["after"]["ticketRef"] == "SUP-1042"
    assert entry["after"]["durationMinutes"] == 60
    assert entry["after"]["expiresAt"] and entry["after"]["readOnly"] is True
    platform = owner_sql(
        "SELECT actor_type, after->>'tenantId' FROM audit_log WHERE tenant_id IS NULL "
        "AND target_id = %s",
        g["id"],
    )
    assert platform == [("staff", tenant)]

    listed = (await client.get(BASE, headers=staff)).json()
    mine = next(x for x in listed if x["id"] == g["id"])
    assert mine["requests"] == 3 and mine["status"] == "active"

    # A second grant for the same workspace while one is active is refused.
    r = await client.post(
        BASE,
        json={"tenantId": tenant, "justification": JUSTIFICATION, "ticketRef": "SUP-1"},
        headers=staff,
    )
    assert r.status_code == 409 and r.json()["code"] == "support_access_active"

    # Revoke: access stops at once and the tenant sees the end.
    r = await client.post(f"{BASE}/{g['id']}/end", headers=staff)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ended" and r.json()["endedByEmail"] == staff_email
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 404
    assert (await client.get("/v1/me", headers=staff)).json()["memberships"] == []
    r = await client.post(f"{BASE}/{g['id']}/end", headers=staff)
    assert r.status_code == 409 and r.json()["code"] == "support_access_inactive"
    ended = [
        e
        for e in await audit_entries(client, tenant, admin)
        if e["action"] == "support_access.ended"
    ]
    assert len(ended) == 1 and ended[0]["after"]["requests"] == 3
    assert (await client.post(f"{BASE}/{uuid.uuid4()}/end", headers=staff)).status_code == 404


async def test_grants_expire_on_their_own(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, _, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    g = await grant(client, staff, tenant, durationMinutes=120)
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 200
    owner_sql(
        "UPDATE support_access_grant SET created_at = now() - interval '3 hours', "
        "expires_at = now() - interval '1 hour' WHERE id = %s",
        g["id"],
    )
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 404
    assert (await client.get("/v1/me", headers=staff)).json()["memberships"] == []
    listed = (await client.get(BASE, headers=staff)).json()
    assert next(x for x in listed if x["id"] == g["id"])["status"] == "expired"
    r = await client.post(f"{BASE}/{g['id']}/end", headers=staff)
    assert r.status_code == 409
    # A new grant may be asked for once the old one has expired.
    await grant(client, staff, tenant)


async def test_duration_and_justification_limits(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, _, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    payload = {"tenantId": tenant, "justification": JUSTIFICATION}
    r = await client.post(BASE, json=payload, headers=staff)
    assert r.status_code == 422, "a ticket reference is required"
    for body in (
        {"durationMinutes": 121},
        {"durationMinutes": 14},
        {"justification": "   too short   "},
        {"ticketRef": "x" * 101},
        {"ticketRef": "   "},
        {"ticketRef": None},
    ):
        payload = {"tenantId": tenant, "justification": JUSTIFICATION, "ticketRef": "SUP-1", **body}
        r = await client.post(BASE, json=payload, headers=staff)
        assert r.status_code == 422, body
    payload = {"tenantId": str(uuid.uuid4()), "justification": JUSTIFICATION, "ticketRef": "SUP-1"}
    assert (await client.post(BASE, json=payload, headers=staff)).status_code == 404
    # The database holds the same limits (docs/11b §7.4: at most 2 hours, a ticket).
    insert = (
        "INSERT INTO support_access_grant (id, tenant_id, tenant_name, staff_user_id, "
        "justification, ticket_ref, expires_at) SELECT %s, %s, 'W', user_id, %s, %s, "
        "now() + CAST(%s AS interval) FROM membership WHERE tenant_id = %s"
    )
    for ticket, length in (("SUP-1", "121 minutes"), (None, "1 hour")):
        with pytest.raises(psycopg.errors.IntegrityError):
            owner_sql(insert, uuid.uuid4(), tenant, JUSTIFICATION, ticket, length, tenant)


async def test_writes_are_refused_and_recorded(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, admin, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    g = await grant(client, staff, tenant)
    writes = [
        ("PATCH", f"/v1/tenants/{tenant}", {"name": "Hijacked"}),
        ("POST", f"/v1/tenants/{tenant}/invites", {"email": "x@example.com", "role": "admin"}),
        (
            "PUT",
            f"/v1/tenants/{tenant}/notification-preferences/me",
            {"choices": []},
        ),  # a viewer route
        ("DELETE", f"/v1/tenants/{tenant}/budgets/{uuid.uuid4()}", None),
        ("POST", f"/v1/tenants/{tenant}/exports", {"kind": "costs"}),
    ]
    for method, path, body in writes:
        r = await client.request(method, path, json=body, headers=staff)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["code"] == "support_read_only"
    # Admin-only reads are refused too: a grant is a viewer.
    r = await client.get(f"/v1/tenants/{tenant}/audit-log", headers=staff)
    assert r.status_code == 403 and r.json()["code"] == "support_read_only"
    assert (await client.get(f"/v1/tenants/{tenant}", headers=admin)).json()[
        "name"
    ] == "Lagos Ledger"
    refused = owner_sql(
        "SELECT count(*) FROM support_access_request WHERE grant_id = %s AND NOT allowed",
        g["id"],
    )[0][0]
    assert refused == len(writes) + 1


async def test_reads_run_in_a_read_only_transaction(client: AsyncClient, login, app) -> None:
    tenant, _, _ = await workspace(client, login)
    async with get_sessionmaker()() as session:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            async with session.begin():
                await scope(session, tenant_id=uuid.UUID(tenant))
                await session.execute(text("SELECT 1"))
                await service.make_read_only(session)
                await session.execute(
                    text("UPDATE tenant SET name = 'x' WHERE id = :t"), {"t": tenant}
                )


async def test_access_is_limited_to_the_granted_workspace(
    client: AsyncClient, login, staff_email, outbox
) -> None:
    tenant, _, _ = await workspace(client, login)
    other, _, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    await grant(client, staff, tenant)
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 200
    for path in ("", "/members", "/costs/summary"):
        r = await client.get(f"/v1/tenants/{other}{path}", headers=staff)
        assert r.status_code == 404, path
    # Another staff member does not inherit the grant.
    colleague = f"colleague-{secrets.token_hex(3)}@cloudoptics.example"
    settings = get_settings()
    settings.platform_admin_emails += f",{colleague}"
    peer = await login(colleague, mfa=True)
    assert (await client.get(f"/v1/tenants/{tenant}", headers=peer)).status_code == 404
    assert (await client.get("/v1/me", headers=peer)).json()["memberships"] == []
    # ...but sees it in the console, and may end it.
    assert any(x["tenantId"] == tenant for x in (await client.get(BASE, headers=peer)).json())


async def test_non_staff_are_refused(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, admin, _ = await workspace(client, login)
    body = {"tenantId": tenant, "justification": JUSTIFICATION, "ticketRef": "SUP-1"}
    for headers in (admin, await login(mfa=True)):
        assert (await client.post(BASE, json=body, headers=headers)).status_code == 403
        assert (await client.get(BASE, headers=headers)).status_code == 403
    # A staff address whose owner has not confirmed it is not staff.
    staff = await login(staff_email, mfa=True)
    verify = DevVerifier.verify

    async def unconfirmed(self: DevVerifier, token: str) -> Any:
        return dataclasses.replace(await verify(self, token), email_verified=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DevVerifier, "verify", unconfirmed)
        assert (await client.post(BASE, json=body, headers=staff)).status_code == 403

    # Someone taken off the staff list loses an active grant at once.
    g = await grant(client, staff, tenant)
    settings = get_settings()
    settings.platform_admin_emails = ""
    assert (await client.get(f"/v1/tenants/{tenant}", headers=staff)).status_code == 404
    me = (await client.get("/v1/me", headers=staff)).json()
    assert me["staff"] is False and me["memberships"] == []
    assert owner_sql(
        "SELECT count(*) FROM support_access_request WHERE grant_id = %s", g["id"]
    ) == [(0,)]


async def test_mfa_is_required(client: AsyncClient, login, staff_email, outbox) -> None:
    tenant, _, _ = await workspace(client, login)
    without = await login(staff_email)
    body = {"tenantId": tenant, "justification": JUSTIFICATION, "ticketRef": "SUP-1"}
    for r in (
        await client.post(BASE, json=body, headers=without),
        await client.get(BASE, headers=without),
    ):
        assert r.status_code == 401 and r.json()["code"] == "mfa_required"
    staff = await login(staff_email, mfa=True)
    g = await grant(client, staff, tenant)
    # The grant is used from a session without MFA: refused.
    r = await client.get(f"/v1/tenants/{tenant}", headers=without)
    assert r.status_code == 401 and r.json()["code"] == "mfa_required"
    r = await client.post(f"{BASE}/{g['id']}/end", headers=without)
    assert r.status_code == 401


def _fill(path: str) -> str:
    path = path.replace("{month}", "2026-10")
    return re.sub(r"\{[a-z_]+\}", lambda m: str(uuid.uuid4()), path)


async def test_every_workspace_route_is_read_only_under_support_access(
    app, client: AsyncClient, login, staff_email, outbox
) -> None:
    """Every GET under /v1/tenants/{id} either answers or refuses (never fails on the
    read-only transaction); every other method is refused before it runs."""
    tenant, _, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    await grant(client, staff, tenant)
    prefix = "/v1/tenants/{tenant_id}"
    routes = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith(prefix)}
    assert len(routes) > 40
    for route, ops in routes.items():
        path = prefix.replace("{tenant_id}", tenant) + _fill(route[len(prefix) :])
        for method in (m.upper() for m in ops):
            r = await client.request(method, path, headers=staff)
            if method in ("GET", "HEAD"):
                assert r.status_code in (200, 202, 204, 403, 404, 409, 410, 422), (
                    method,
                    route,
                    r.status_code,
                    r.text,
                )
                if r.status_code == 403:
                    assert r.json()["code"] == "support_read_only", route
            else:
                assert r.status_code == 403, (method, route, r.text)
                assert r.json()["code"] == "support_read_only", route
