"""Send the connect step to the cloud admin (connect invites, ADR-022).

The session a link opens is checked against the app's own route table, as the membership
test does (#100): every route outside ``/v1/connect-invite`` must refuse it with 401 or 403,
and every route inside must take it through ``invite_session``, so a new route is covered the
day it is added. Then the life of an invite: opened, connected, reused, expired, revoked,
another workspace, the sender losing admin, and the daily send limit.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fake_aws import healthy_clients
from fake_gcp import FakeGcp, install, uninstall
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from httpx import AsyncClient
from test_access_connect import account, callback_body, link_params
from test_data_deletion import workspace
from test_gcp import create_body
from test_invites import Outbox
from test_route_membership import _calls, _fill

from cloudoptics.connectors import handoff
from cloudoptics.notifications.email import set_mailer
from cloudoptics.platform.settings import get_settings

Sql = Callable[..., list[tuple[Any, ...]]]
LINK = re.compile(r"/connect-invite/([A-Za-z0-9_-]{20,})")
PREFIX = "/v1/connect-invite"

# Routes that need no signed-in user at all, so the session's header means nothing to them;
# none of them reads workspace data. (method, path) -> why.
PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "liveness",
    ("GET", "/readyz"): "readiness",
    ("GET", "/v1/auth/config"): "how the web app signs in",
    ("POST", "/v1/dev/token"): "development sign-in (dev auth mode only)",
    ("POST", "/v1/cfn/callback"): "the AWS stack's callback, proven by its External ID",
    ("GET", "/v1/azure/consent"): "the page Entra sends the browser back to; reads nothing",
    ("POST", "/v1/site/visits"): "the public site's anonymous page views",
}


@pytest.fixture
def outbox() -> Iterator[Outbox]:
    box = Outbox()
    set_mailer(box)
    yield box
    set_mailer(None)


@pytest.fixture
def gcp() -> Iterator[FakeGcp]:
    fake = FakeGcp().bill().grant_all()
    install(fake)
    yield fake
    uninstall()


def _needs(dependant: Dependant, name: str) -> bool:
    return any(getattr(c, "__name__", "") == name for c in _calls(dependant))


def _routes(app) -> list[tuple[str, str, Dependant]]:  # type: ignore[no-untyped-def]
    found = []
    for route in iter_route_contexts(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None or not route.path:
            continue
        for method in sorted(route.methods or ()):
            if method != "HEAD":
                found.append((method, route.path, dependant))
    return found


def link_token(box: Outbox, to: str) -> str:
    [email] = [e for e in box.sent if e.to == [to] and e.tag == "connect-invite"][-1:]
    match = LINK.search(email.text)
    assert match, email.text
    return match.group(1)


async def send(
    client: AsyncClient, tenant: str, headers: dict[str, str], email: str, provider: str
) -> Any:
    return await client.post(
        f"/v1/tenants/{tenant}/connect-invites",
        json={"email": email, "provider": provider},
        headers=headers,
    )


async def opened(client: AsyncClient, box: Outbox, to: str) -> tuple[dict[str, str], Any]:
    r = await client.post(f"{PREFIX}/open", json={"token": link_token(box, to)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"ConnectInvite {r.json()['sessionToken']}"}, r.json()["invite"]


async def statuses(client: AsyncClient, tenant: str, headers: dict[str, str]) -> list[str]:
    r = await client.get(f"/v1/tenants/{tenant}/connect-invites", headers=headers)
    assert r.status_code == 200, r.text
    return [i["status"] for i in r.json()]


def admin_email() -> str:
    return f"cloud-admin-{uuid.uuid4().hex[:6]}@example.com"


# --- the session's reach --------------------------------------------------------------------


def test_every_invite_route_takes_the_session_and_nothing_else_does(app) -> None:  # type: ignore[no-untyped-def]
    routes = _routes(app)
    inside = [(m, p) for m, p, d in routes if p.startswith(PREFIX)]
    assert ("POST", f"{PREFIX}/open") in inside and len(inside) > 10
    missing = [
        f"{m} {p}"
        for m, p, d in routes
        if p.startswith(PREFIX) and p != f"{PREFIX}/open" and not _needs(d, "invite_session")
    ]
    assert missing == []
    leaked = [f"{m} {p}" for m, p, d in routes if not p.startswith(PREFIX) and _needs(d, "invite_session")]
    assert leaked == []
    # Every other route either needs a signed-in user or is a listed public one.
    unlisted = [
        f"{m} {p}"
        for m, p, d in routes
        if not p.startswith(PREFIX)
        and not _needs(d, "current_user")
        and (m, p) not in PUBLIC
        and not p.startswith(("/docs", "/openapi", "/redoc"))
    ]
    assert unlisted == [], "a new public route: list it in PUBLIC with a reason"
    assert set(PUBLIC) - {(m, p) for m, p, _ in routes} == set(), "stale PUBLIC entry"


async def test_the_session_is_refused_on_every_other_route(
    app,  # type: ignore[no-untyped-def]
    client: AsyncClient,
    login,
    outbox: Outbox,
) -> None:
    tenant, owner = await workspace(client, login, "Acme Ltd")
    to = admin_email()
    assert (await send(client, tenant, owner, to, "gcp")).status_code == 201
    link = link_token(outbox, to)
    session, _ = await opened(client, outbox, to)
    raw = session["Authorization"].split(" ", 1)[1]
    # The session token, and the link's own token, in every way a client might send them.
    attempts = [session, {"Authorization": f"Bearer {raw}"}, {"Authorization": f"Bearer {link}"}]

    wrong = []
    for method, path, dependant in _routes(app):
        if path.startswith(PREFIX) or not _needs(dependant, "current_user"):
            continue
        for headers in attempts:
            r = await client.request(method, _fill(path, tenant), headers=headers, json={})
            if r.status_code not in (401, 403):
                wrong.append(f"{method} {path}: {r.status_code}")
    assert wrong == []
    # The session still works on its own routes.
    assert (await client.get(f"{PREFIX}/session", headers=session)).status_code == 200


async def test_invite_routes_need_a_session(client: AsyncClient, login) -> None:
    _, owner = await workspace(client, login, "Acme Ltd")
    for headers in ({}, owner, {"Authorization": "ConnectInvite nope"}):
        r = await client.get(f"{PREFIX}/session", headers=headers)
        assert r.status_code == 401, r.text
    r = await client.post(f"{PREFIX}/open", json={"token": "x" * 43})
    assert r.status_code == 404


# --- the hand-off, end to end ---------------------------------------------------------------


async def test_google_cloud_hand_off_connects_without_joining(
    client: AsyncClient, login, outbox: Outbox, owner_sql: Sql, gcp: FakeGcp
) -> None:
    tenant, owner = await workspace(client, login, "Acme Ltd")
    to = admin_email()
    r = await send(client, tenant, owner, to, "gcp")
    assert r.status_code == 201, r.text
    sent = r.json()
    assert sent["status"] == "sent" and sent["emailSent"] is True and sent["provider"] == "gcp"
    [email] = [e for e in outbox.sent if e.to == [to]]
    assert "Acme Ltd" in email.subject and "Read-only" in email.text
    assert "do not join the workspace" in email.text and "Billing Account Administrator" in email.text
    token = link_token(outbox, to)
    stored = owner_sql("SELECT token_hash FROM connect_invite WHERE id = %s", sent["id"])
    assert bytes(stored[0][0]) == handoff.token_hash(token)  # only the hash is kept
    assert token.encode() not in bytes(stored[0][0])

    session, view = await opened(client, outbox, to)
    assert view["status"] == "opened" and view["workspaceName"] == "Acme Ltd"
    assert view["requestedBy"]["email"] and view["connection"] is None
    assert await statuses(client, tenant, owner) == ["opened"]

    # Another cloud's actions are refused; this one's work.
    r = await client.post(f"{PREFIX}/session/aws/link", headers=session)
    assert r.status_code == 403 and r.json()["code"] == "invite_provider"
    r = await client.post(f"{PREFIX}/session/gcp", json=create_body(gcp), headers=session)
    assert r.status_code == 201, r.text
    setup = r.json()
    assert setup["serviceAccount"]
    again = await client.get(f"{PREFIX}/session/gcp", headers=session)
    assert again.status_code == 200 and again.json()["serviceAccount"] == setup["serviceAccount"]
    # A second connection while the first is being set up is refused.
    r = await client.post(f"{PREFIX}/session/gcp", json=create_body(gcp), headers=session)
    assert r.status_code == 409 and r.json()["code"] == "invite_in_progress"

    gcp.customer_setup(setup["serviceAccount"])
    r = await client.post(f"{PREFIX}/session/check", headers=session)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    view = (await client.get(f"{PREFIX}/session", headers=session)).json()
    assert view["status"] == "connected" and view["connection"]["status"] == "healthy"
    assert await statuses(client, tenant, owner) == ["connected"]

    # The sender is told once; the admin never joined; every step names the invite.
    told = [e for e in outbox.sent if e.tag == "connect-invite-connected"]
    assert len(told) == 1 and to in told[0].subject
    await statuses(client, tenant, owner)
    assert len([e for e in outbox.sent if e.tag == "connect-invite-connected"]) == 1
    members = (await client.get(f"/v1/tenants/{tenant}/members", headers=owner)).json()
    assert len(members) == 1
    actions = owner_sql(
        "SELECT action, actor_type FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant
    )
    assert ("connect_invite.sent", "user") in actions
    for action in ("connect_invite.opened", "connection.created", "connect_invite.connected"):
        assert (action, "connect_invite") in actions, action
    assert not any(t == "user" and a.startswith("connection.") for a, t in actions)
    by = owner_sql(
        "SELECT c.created_by = i.invited_by FROM cloud_connection c "
        "JOIN connect_invite i ON i.connection_id = c.id WHERE i.id = %s",
        sent["id"],
    )
    assert by == [(True,)]
    log = (await client.get(f"/v1/tenants/{tenant}/audit-log", headers=owner)).json()
    named = {e["actor"]["name"] for e in log["entries"] if e["actor"]["type"] == "connect_invite"}
    assert named == {f"connect invite {to}"} and log["verified"] is True

    # Used: the link no longer opens, and the session can only read and check again.
    r = await client.post(f"{PREFIX}/open", json={"token": token})
    assert r.status_code == 410 and r.json()["code"] == "invite_used"
    r = await client.post(f"{PREFIX}/session/gcp", json=create_body(gcp), headers=session)
    assert r.status_code == 409 and r.json()["code"] == "invite_used"
    assert (await client.post(f"{PREFIX}/session/check", headers=session)).status_code == 200
    r = await client.delete(f"/v1/tenants/{tenant}/connect-invites/{sent['id']}", headers=owner)
    assert r.status_code == 409


async def test_aws_hand_off_follows_the_stack(
    client: AsyncClient, login, outbox: Outbox, quick_create, fake_aws
) -> None:
    acct = account()
    tenant, owner = await workspace(client, login, "Acme Ltd")
    to = admin_email()
    assert (await send(client, tenant, owner, to, "aws")).status_code == 201
    session, _ = await opened(client, outbox, to)
    r = await client.post(f"{PREFIX}/session/aws/link", headers=session)
    assert r.status_code == 201, r.text
    setup = r.json()
    params = link_params(setup["quickCreateUrl"])
    assert params["param_TenantId"] == tenant
    assert setup["tenantId"] == tenant  # for setting the stack up by hand
    intent = setup["intent"]["id"]
    r = await client.get(f"{PREFIX}/session/aws/link/{uuid.uuid4()}", headers=session)
    assert r.status_code == 404  # only this link's own

    fake_aws.clients = healthy_clients(acct)
    r = await client.post("/v1/cfn/callback", json=callback_body(params, acct))
    assert r.status_code == 200, r.text
    r = await client.get(f"{PREFIX}/session/aws/link/{intent}", headers=session)
    assert r.json()["status"] == "connected"
    view = (await client.get(f"{PREFIX}/session", headers=session)).json()
    assert view["status"] == "connected" and view["connection"]["awsAccountId"] == acct
    assert await statuses(client, tenant, owner) == ["connected"]


# --- ends of the line -----------------------------------------------------------------------


async def test_expiry(client: AsyncClient, login, outbox: Outbox, owner_sql: Sql) -> None:
    tenant, owner = await workspace(client, login, "Acme Ltd")
    to = admin_email()
    invite = (await send(client, tenant, owner, to, "azure")).json()
    session, _ = await opened(client, outbox, to)
    owner_sql(
        "UPDATE connect_invite SET expires_at = now() - interval '1 minute' WHERE id = %s",
        invite["id"],
    )
    r = await client.get(f"{PREFIX}/session", headers=session)
    assert r.status_code == 410 and r.json()["code"] == "invite_expired"
    r = await client.post(f"{PREFIX}/open", json={"token": link_token(outbox, to)})
    assert r.status_code == 410
    # The sweep tells the sender once; the list shows it as expired.
    assert await handoff.sweep() >= 1
    assert await statuses(client, tenant, owner) == ["expired"]
    assert await handoff.sweep() == 0
    told = [e for e in outbox.sent if e.tag == "connect-invite-expired"]
    assert len(told) == 1 and to in told[0].subject
    expired = owner_sql(
        "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action = 'connect_invite.expired'",
        tenant,
    )
    assert expired == [(1,)]


async def test_revocation_and_replacement(client: AsyncClient, login, outbox: Outbox) -> None:
    tenant, owner = await workspace(client, login, "Acme Ltd")
    to = admin_email()
    first = (await send(client, tenant, owner, to, "digitalocean")).json()
    old_link = link_token(outbox, to)
    session, _ = await opened(client, outbox, to)
    # Opening the link again ends the first session.
    second_session, _ = await opened(client, outbox, to)
    assert (await client.get(f"{PREFIX}/session", headers=session)).status_code == 401
    assert (await client.get(f"{PREFIX}/session", headers=second_session)).status_code == 200

    # Sending the same cloud to the same address again replaces the request.
    second = (await send(client, tenant, owner, to, "digitalocean")).json()
    assert (await client.post(f"{PREFIX}/open", json={"token": old_link})).status_code == 410
    assert await statuses(client, tenant, owner) == ["sent", "revoked"]
    assert first["id"] != second["id"]

    session, _ = await opened(client, outbox, to)
    r = await client.delete(f"/v1/tenants/{tenant}/connect-invites/{second['id']}", headers=owner)
    assert r.status_code == 204
    r = await client.get(f"{PREFIX}/session", headers=session)
    assert r.status_code in (401, 410)
    r = await client.post(f"{PREFIX}/open", json={"token": link_token(outbox, to)})
    assert r.status_code == 410 and r.json()["code"] == "invite_revoked"
    again = await client.delete(f"/v1/tenants/{tenant}/connect-invites/{second['id']}", headers=owner)
    assert again.status_code == 204


async def test_other_workspaces_and_roles(
    client: AsyncClient, login, outbox: Outbox, owner_sql: Sql
) -> None:
    tenant, owner = await workspace(client, login, "Acme Ltd")
    other, other_owner = await workspace(client, login, "Other Ltd")
    to = admin_email()
    invite = (await send(client, tenant, owner, to, "gcp")).json()
    # Another workspace's admin sees nothing of it and cannot revoke it.
    assert await statuses(client, other, other_owner) == []
    r = await client.delete(f"/v1/tenants/{other}/connect-invites/{invite['id']}", headers=other_owner)
    assert r.status_code == 404
    r = await client.get(f"/v1/tenants/{tenant}/connect-invites", headers=other_owner)
    assert r.status_code == 404

    # Members and viewers cannot send.
    member_email = f"member-{uuid.uuid4().hex[:6]}@example.com"
    member = await login(member_email)
    member_id = (await client.get("/v1/me", headers=member)).json()["id"]
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) VALUES (%s, %s, 'member')",
        tenant,
        member_id,
    )
    assert (await send(client, tenant, member, admin_email(), "aws")).status_code == 403

    # The session sees its own workspace's invite only, and stops when its sender is no
    # longer an admin.
    session, view = await opened(client, outbox, to)
    assert view["workspaceName"] == "Acme Ltd"
    sender = (await client.get("/v1/me", headers=owner)).json()["id"]
    owner_sql(
        "UPDATE membership SET role = 'viewer' WHERE tenant_id = %s AND user_id = %s",
        tenant,
        sender,
    )
    r = await client.get(f"{PREFIX}/session", headers=session)
    assert r.status_code == 403 and r.json()["code"] == "invite_sender_not_admin"


async def test_sends_are_limited_per_workspace(
    client: AsyncClient, login, outbox: Outbox, owner_sql: Sql
) -> None:
    settings = get_settings()
    limit = settings.connect_invites_per_day
    settings.connect_invites_per_day = 2
    tenant, owner = await workspace(client, login, "Acme Ltd")
    other, other_owner = await workspace(client, login, "Other Ltd")
    try:
        for _ in range(2):
            assert (await send(client, tenant, owner, admin_email(), "aws")).status_code == 201
        r = await send(client, tenant, owner, admin_email(), "aws")
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        assert r.json()["code"] == "rate_limited"
        # Another workspace has its own count.
        assert (await send(client, other, other_owner, admin_email(), "gcp")).status_code == 201
        # A day later the workspace may send again.
        owner_sql(
            "UPDATE connect_invite SET created_at = now() - interval '25 hours' "
            "WHERE tenant_id = %s",
            tenant,
        )
        assert (await send(client, tenant, owner, admin_email(), "aws")).status_code == 201
    finally:
        settings.connect_invites_per_day = limit


async def test_a_failed_email_is_shown(client: AsyncClient, login) -> None:
    set_mailer(Outbox(fail=True))
    try:
        tenant, owner = await workspace(client, login, "Acme Ltd")
        r = await send(client, tenant, owner, admin_email(), "aws")
        assert r.status_code == 201 and r.json()["emailSent"] is False
    finally:
        set_mailer(None)
