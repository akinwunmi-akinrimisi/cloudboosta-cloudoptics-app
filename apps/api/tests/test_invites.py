"""Invites and member management (WBS ADM-3, PRD US-8.1)."""

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from test_identity import create_tenant

from cloudoptics.identity.auth import DevVerifier
from cloudoptics.notifications.email import Email, EmailError, set_mailer


class Outbox:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Email] = []
        self.fail = fail

    async def send(self, email: Email) -> None:
        if self.fail:
            raise EmailError("postmark_422: test")
        self.sent.append(email)


@pytest.fixture
def outbox() -> Iterator[Outbox]:
    box = Outbox()
    set_mailer(box)
    yield box
    set_mailer(None)


async def invite(
    client: AsyncClient, tenant: dict[str, Any], headers: dict[str, str], email: str, role: str
) -> Any:
    return await client.post(
        f"/v1/tenants/{tenant['id']}/invites", json={"email": email, "role": role}, headers=headers
    )


async def test_invited_viewer_joins_on_first_sign_in(
    client: AsyncClient, login, outbox: Outbox
) -> None:
    admin = await login("owner@example.com")
    tenant = await create_tenant(client, admin, name="Lagos Ledger")
    r = await invite(client, tenant, admin, "Claude-Review@Example.com", "viewer")
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["email"], body["role"], body["status"]) == (
        "claude-review@example.com",
        "viewer",
        "pending",
    )
    assert body["emailSent"] is True and body["invitedByEmail"] == "owner@example.com"
    [mail] = outbox.sent
    assert mail.to == ["claude-review@example.com"] and "Lagos Ledger" in mail.subject

    guest = await login("claude-review@example.com")
    me = (await client.get("/v1/me", headers=guest)).json()
    assert [(m["tenantName"], m["role"]) for m in me["memberships"]] == [("Lagos Ledger", "viewer")]

    # A viewer reads and cannot change anything.
    assert (await client.get(f"/v1/tenants/{tenant['id']}", headers=guest)).status_code == 200
    r = await client.patch(f"/v1/tenants/{tenant['id']}", json={"name": "x"}, headers=guest)
    assert r.status_code == 403
    assert (await invite(client, tenant, guest, "x@example.com", "admin")).status_code == 403

    # Used once: the invite is gone and the join is in the audit log.
    assert (await client.get(f"/v1/tenants/{tenant['id']}/invites", headers=admin)).json() == []
    log = (await client.get(f"/v1/tenants/{tenant['id']}/audit-log", headers=admin)).json()
    actions = [e["action"] for e in log["entries"]]
    assert "invite.created" in actions and "member.joined" in actions


async def test_an_unconfirmed_email_does_not_join(
    client: AsyncClient, login, outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await login()
    tenant = await create_tenant(client, admin)
    await invite(client, tenant, admin, "unconfirmed@example.com", "admin")
    verify = DevVerifier.verify

    async def unconfirmed(self: DevVerifier, token: str) -> Any:
        return dataclasses.replace(await verify(self, token), email_verified=False)

    monkeypatch.setattr(DevVerifier, "verify", unconfirmed)
    guest = await login("unconfirmed@example.com")
    assert (await client.get("/v1/me", headers=guest)).json()["memberships"] == []
    monkeypatch.setattr(DevVerifier, "verify", verify)
    assert len((await client.get("/v1/me", headers=guest)).json()["memberships"]) == 1


async def test_expired_and_revoked_invites_do_not_join(
    client: AsyncClient, login, outbox: Outbox, owner_sql
) -> None:
    admin = await login()
    tenant = await create_tenant(client, admin)
    late = (await invite(client, tenant, admin, "late@example.com", "member")).json()
    owner_sql(
        "UPDATE invite SET expires_at = now() - interval '1 minute' WHERE id = %s", late["id"]
    )
    [listed] = (await client.get(f"/v1/tenants/{tenant['id']}/invites", headers=admin)).json()
    assert listed["status"] == "expired"
    gone = (await invite(client, tenant, admin, "gone@example.com", "member")).json()
    r = await client.delete(f"/v1/tenants/{tenant['id']}/invites/{gone['id']}", headers=admin)
    assert r.status_code == 204
    r = await client.delete(f"/v1/tenants/{tenant['id']}/invites/{gone['id']}", headers=admin)
    assert r.status_code == 404
    for email in ("late@example.com", "gone@example.com"):
        headers = await login(email)
        assert (await client.get("/v1/me", headers=headers)).json()["memberships"] == []


async def test_inviting_again_replaces_the_open_invite(
    client: AsyncClient, login, outbox: Outbox
) -> None:
    admin = await login()
    tenant = await create_tenant(client, admin)
    await invite(client, tenant, admin, "kofi@example.com", "viewer")
    await invite(client, tenant, admin, "kofi@example.com", "member")
    [open_invite] = (await client.get(f"/v1/tenants/{tenant['id']}/invites", headers=admin)).json()
    assert open_invite["role"] == "member"
    me = (await client.get("/v1/me", headers=await login("kofi@example.com"))).json()
    assert [m["role"] for m in me["memberships"]] == ["member"]
    r = await invite(client, tenant, admin, "kofi@example.com", "admin")
    assert r.status_code == 409


async def test_a_failed_invite_email_still_creates_the_invite(client: AsyncClient, login) -> None:
    set_mailer(Outbox(fail=True))
    try:
        admin = await login()
        tenant = await create_tenant(client, admin)
        r = await invite(client, tenant, admin, "ama@example.com", "viewer")
    finally:
        set_mailer(None)
    assert r.status_code == 201 and r.json()["emailSent"] is False


async def test_admins_change_roles_and_remove_members_but_keep_an_admin(
    client: AsyncClient, login, outbox: Outbox
) -> None:
    admin = await login("first-admin@example.com")
    tenant = await create_tenant(client, admin)
    base = f"/v1/tenants/{tenant['id']}"
    await invite(client, tenant, admin, "second@example.com", "viewer")
    second = await login("second@example.com")
    await client.get("/v1/me", headers=second)
    members = {
        m["email"]: m["userId"] for m in (await client.get(f"{base}/members", headers=admin)).json()
    }

    r = await client.patch(
        f"{base}/members/{members['first-admin@example.com']}",
        json={"role": "member"},
        headers=admin,
    )
    assert r.status_code == 409  # the only admin
    r = await client.patch(
        f"{base}/members/{members['second@example.com']}", json={"role": "admin"}, headers=admin
    )
    assert r.status_code == 204
    r = await client.delete(f"{base}/members/{members['first-admin@example.com']}", headers=second)
    assert r.status_code == 204
    r = await client.delete(f"{base}/members/{members['second@example.com']}", headers=second)
    assert r.status_code == 409
    assert (await client.get(base, headers=admin)).status_code == 404  # removed
    r = await client.delete(f"{base}/members/{members['first-admin@example.com']}", headers=second)
    assert r.status_code == 404

    log = (await client.get(f"{base}/audit-log", headers=second)).json()
    actions = [e["action"] for e in log["entries"]]
    assert "member.role_changed" in actions and "member.removed" in actions


async def test_invites_stay_inside_their_workspace(
    client: AsyncClient, login, outbox: Outbox
) -> None:
    alice = await login()
    bob = await login()
    a = await create_tenant(client, alice, name="A")
    b = await create_tenant(client, bob, name="B")
    made = (await invite(client, a, alice, "shared@example.com", "viewer")).json()
    assert (await client.get(f"/v1/tenants/{b['id']}/invites", headers=bob)).json() == []
    assert (await client.get(f"/v1/tenants/{a['id']}/invites", headers=bob)).status_code == 404
    r = await client.delete(f"/v1/tenants/{b['id']}/invites/{made['id']}", headers=bob)
    assert r.status_code == 404
    me = (await client.get("/v1/me", headers=await login("shared@example.com"))).json()
    assert [m["tenantName"] for m in me["memberships"]] == ["A"]

    # An invite to an admin's own email from another workspace is not listed in theirs.
    carol_email = "carol@example.com"
    carol = await login(carol_email)
    c = await create_tenant(client, carol, name="C")
    await invite(client, a, alice, carol_email, "viewer")
    assert (await client.get(f"/v1/tenants/{c['id']}/invites", headers=carol)).json() == []
