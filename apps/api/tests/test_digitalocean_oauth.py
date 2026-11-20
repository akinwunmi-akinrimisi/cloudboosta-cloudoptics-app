"""Connect DigitalOcean by signing in (ADR-021, OAuth) against the fake DigitalOcean: the
sign-in state (tampering, replay, expiry, another user or workspace), the code exchange, the
access checks, one workspace per team, renewal (and two renewals racing), a refused renewal
raising the connection alert, revocation on disconnect, and that no token leaks."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fake_digitalocean import (
    CLIENT_ID,
    CLIENT_SECRET,
    READ_SCOPES,
    WRITE_SCOPES,
    FakeDigitalOcean,
    parse,
)
from httpx import AsyncClient
from pydantic import SecretStr
from test_data_deletion import member, workspace
from test_digitalocean import assert_secrets_kept_secret

from cloudoptics.connectors import jobs as connector_jobs
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.digitalocean import client as do_client
from cloudoptics.providers.digitalocean import oauth

Sql = Callable[..., list[tuple[Any, ...]]]
TODAY = datetime.now(UTC).date()


@pytest.fixture
def do_oauth(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDigitalOcean]:
    settings = get_settings()
    monkeypatch.setattr(settings, "do_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "do_oauth_client_secret", SecretStr(CLIENT_SECRET))
    fake = FakeDigitalOcean(today=TODAY, redirect_uri=settings.do_oauth_callback_url)
    do_client.set_transport(fake.transport())
    yield fake
    do_client.set_transport(None)


def _base(tenant: str) -> str:
    return f"/v1/tenants/{tenant}/connections/digitalocean/oauth"


async def _start(client: AsyncClient, tenant: str, headers: dict[str, str]) -> dict[str, str]:
    r = await client.post(f"{_base(tenant)}/start", headers=headers)
    assert r.status_code == 201, r.text
    path, query = parse(r.json()["authorizeUrl"])
    assert path == "/v1/oauth/authorize"
    return query


async def _sign_in(
    client: AsyncClient, tenant: str, headers: dict[str, str], fake: FakeDigitalOcean
) -> dict[str, str]:
    """Start, then the customer signs in, picks the team and approves at DigitalOcean."""
    _, back = parse(fake.authorize(await _start(client, tenant, headers)))
    return back


async def _finish(
    client: AsyncClient, tenant: str, headers: dict[str, str], back: dict[str, str]
) -> httpx.Response:
    return await client.post(
        f"{_base(tenant)}/callback",
        json={"code": back["code"], "state": back["state"]},
        headers=headers,
    )


async def _connect(
    client: AsyncClient, tenant: str, headers: dict[str, str], fake: FakeDigitalOcean
) -> httpx.Response:
    return await _finish(client, tenant, headers, await _sign_in(client, tenant, headers, fake))


def _live_tokens(fake: FakeDigitalOcean, team: str) -> tuple[str, str]:
    """The team's current access and refresh tokens at the fake."""
    for refresh, live in reversed(fake.refresh_tokens.items()):
        if live["team"] == team and live["access"] in fake.tokens:
            return live["access"], refresh
    raise AssertionError("no live tokens")


def _expire_soon(owner_sql: Sql, cid: str, days: int = 2) -> None:
    owner_sql(
        "UPDATE cloud_connection SET token_expires_at = now() + make_interval(days => %s) "
        "WHERE id = %s",
        days,
        cid,
    )


# --- pure parts ---------------------------------------------------------------------------


def test_pkce_challenge_and_the_stored_secret() -> None:
    # RFC 7636, appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth.challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    tokens = oauth.Tokens(
        "doo_v1_" + "a" * 64, "dor_v1_" + "b" * 64, datetime.now(UTC), "read", "team-1", "T"
    )
    secret = oauth.encode(tokens)
    assert oauth.is_oauth_secret(secret)
    assert oauth.access_token(secret) == tokens.access
    assert oauth.refresh_token(secret) == tokens.refresh
    assert oauth.access_token("dop_v1_" + "c" * 64) == "dop_v1_" + "c" * 64
    assert "a" * 64 not in repr(tokens) and "b" * 64 not in repr(tokens)
    assert "a" * 64 not in str(oauth.OAuthError(400, "invalid_grant"))


def test_token_answers_are_checked() -> None:
    now = datetime.now(UTC)
    good = {"access_token": "doo_v1_" + "a" * 64, "refresh_token": "dor_v1_" + "b" * 64}
    t = oauth._tokens({**good, "expires_in": 2592000, "info": {"team_uuid": "x" * 70}}, now)
    assert t.expires_at == now + timedelta(days=30) and t.team_uuid is None
    assert oauth._tokens({**good, "expires_in": "soon"}, now).expires_at == now + timedelta(hours=1)
    for broken in ({"access_token": "short"}, {**good, "refresh_token": None}):
        with pytest.raises(oauth.OAuthError):
            oauth._tokens(broken, now)
    assert oauth.OAuthError(400, "invalid_grant").refused
    assert not oauth.OAuthError(503, "server_error").refused
    assert not oauth.OAuthError(429, "slow_down").refused
    assert not oauth.OAuthError(None, "network:ConnectError").refused


# --- configuration ------------------------------------------------------------------------


async def test_hidden_until_the_oauth_app_is_configured(client: AsyncClient, login: Any) -> None:
    tenant, headers = await workspace(client, login)
    r = await client.get(_base(tenant), headers=headers)
    assert r.status_code == 200 and r.json() == {"enabled": False}
    r = await client.post(f"{_base(tenant)}/start", headers=headers)
    assert r.status_code == 404 and r.json()["code"] == "oauth_not_configured"


async def test_only_admins_sign_in(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    assert (await client.get(_base(tenant), headers=headers)).json() == {"enabled": True}
    viewer = await member(client, login, owner_sql, tenant, "member")
    assert (await client.post(f"{_base(tenant)}/start", headers=viewer)).status_code == 403


# --- signing in ---------------------------------------------------------------------------


async def test_sign_in_connects_and_loads_costs(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    do_oauth: FakeDigitalOcean,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tenant, headers = await workspace(client, login, "Signed In Ltd")
    query = await _start(client, tenant, headers)
    settings = get_settings()
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == settings.do_oauth_callback_url
    assert query["response_type"] == "code"
    assert query["scope"] == " ".join(settings.do_oauth_scope.split())
    assert "billing:read" in query["scope"].split() and "read" not in query["scope"].split()
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 40
    _, back = parse(do_oauth.authorize(query))
    r = await _finish(client, tenant, headers, back)
    assert r.status_code == 201, r.text
    body = r.json()
    cid = body["id"]
    team = body["awsAccountId"]
    assert body["provider"] == "digitalocean" and body["credentialType"] == "oauth"
    assert body["status"] == "healthy"
    assert {c["id"] for c in body["permissionChecks"]} >= {"account", "billing", "read_only"}
    # The documented exchange: client authentication and the PKCE verifier.
    grant, fields = do_oauth.token_requests[-1]
    assert grant == "authorization_code"
    assert {"client_id", "client_secret", "code", "code_verifier", "redirect_uri"} <= set(fields)
    # Costs load as for a pasted token; the team's name comes from the token response.
    months = owner_sql(
        "SELECT count(DISTINCT billing_period) FROM cost_daily WHERE connection_id = %s", cid
    )[0][0]
    assert months == 2
    assert owner_sql("SELECT name FROM cloud_account WHERE connection_id = %s", cid) == [
        ("Ocean Team",)
    ]
    expires = owner_sql("SELECT token_expires_at FROM cloud_connection WHERE id = %s", cid)[0][0]
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)
    created = owner_sql(
        "SELECT after FROM audit_log WHERE tenant_id = %s AND action = 'connection.created'",
        tenant,
    )[0][0]
    assert created["method"] == "sign_in" and created["accountId"] == team
    # Neither token is anywhere but the ciphertext; the user's email is not kept either.
    access, refresh = _live_tokens(do_oauth, team)
    await assert_secrets_kept_secret(
        client, owner_sql, caplog, tenant, headers, cid, [access, refresh]
    )
    rows = owner_sql(
        "SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a WHERE tenant_id = %s",
        tenant,
    )[0][0]
    assert "sammy@example.com" not in rows and "Sammy" not in rows


async def test_state_tampering_replay_and_expiry(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    back = await _sign_in(client, tenant, headers, do_oauth)
    tampered = {
        **back,
        "state": back["state"][:-2] + ("AA" if back["state"][-2:] != "AA" else "BB"),
    }
    r = await _finish(client, tenant, headers, tampered)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    assert (await _finish(client, tenant, headers, {**back, "state": "x"})).status_code == 422
    r = await _finish(client, tenant, headers, back)
    assert r.status_code == 201, r.text
    # Replay: the same state (and code) again.
    r = await _finish(client, tenant, headers, back)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    # Expired: older than 10 minutes.
    late = await _sign_in(client, tenant, headers, do_oauth)
    owner_sql(
        "UPDATE oauth_state SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id = %s AND used_at IS NULL",
        tenant,
    )
    r = await _finish(client, tenant, headers, late)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    # A state is spent even when the exchange then fails: no second try with it.
    bad_code = await _sign_in(client, tenant, headers, do_oauth)
    r = await _finish(client, tenant, headers, {**bad_code, "code": "not-the-code"})
    assert r.status_code == 422 and r.json()["code"] == "oauth_refused"
    r = await _finish(client, tenant, headers, bad_code)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant) == [(1,)]


async def test_a_state_works_only_for_its_user_and_workspace(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login, "First Ltd")
    other_tenant = (
        await client.post(
            "/v1/tenants", json={"name": "Second Ltd", "countryCode": "NG"}, headers=headers
        )
    ).json()["id"]
    back = await _sign_in(client, tenant, headers, do_oauth)
    # The same admin, the other workspace: that workspace cannot see the state (RLS).
    r = await _finish(client, other_tenant, headers, back)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    # Another admin of the same workspace.
    colleague = await member(client, login, owner_sql, tenant, "admin")
    r = await _finish(client, tenant, colleague, back)
    assert r.status_code == 400 and r.json()["code"] == "oauth_state"
    # Someone signed in elsewhere entirely.
    stranger, stranger_headers = await workspace(client, login, "Stranger Ltd")
    r = await _finish(client, stranger, stranger_headers, back)
    assert r.status_code == 400
    # Those attempts did not use it up: its own user and workspace still can.
    r = await _finish(client, tenant, headers, back)
    assert r.status_code == 201, r.text
    for t in (other_tenant, stranger):
        assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", t) == [(0,)]


async def test_cancelled_or_refused_sign_ins_keep_nothing(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    # Write access approved on "Manage permissions": refused, and the token revoked.
    do_oauth.granted = READ_SCOPES | WRITE_SCOPES
    r = await _connect(client, tenant, headers, do_oauth)
    assert r.status_code == 422 and r.json()["code"] == "token_can_write"
    assert len(do_oauth.revoked) == 1 and do_oauth.revoked[0] not in r.text
    # Without billing:read: the access check says which scope is missing.
    do_oauth.granted = READ_SCOPES - {"billing:read"}
    r = await _connect(client, tenant, headers, do_oauth)
    assert r.status_code == 422 and "billing:read" in r.json()["title"]
    assert len(do_oauth.revoked) == 2
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant) == [(0,)]


async def test_the_broad_read_scope_works_too(
    client: AsyncClient,
    login: Any,
    monkeypatch: pytest.MonkeyPatch,
    do_oauth: FakeDigitalOcean,
) -> None:
    """If DigitalOcean grants only ``read`` / ``read write`` to OAuth apps, the setting is
    changed to ``read`` and nothing else changes."""
    monkeypatch.setattr(get_settings(), "do_oauth_scope", "read")
    tenant, headers = await workspace(client, login)
    query = await _start(client, tenant, headers)
    assert query["scope"] == "read"
    _, back = parse(do_oauth.authorize(query))
    r = await _finish(client, tenant, headers, back)
    assert r.status_code == 201 and r.json()["status"] == "healthy"


async def test_without_pkce_the_client_secret_alone_is_sent(
    client: AsyncClient,
    login: Any,
    monkeypatch: pytest.MonkeyPatch,
    do_oauth: FakeDigitalOcean,
) -> None:
    monkeypatch.setattr(get_settings(), "do_oauth_pkce", False)
    tenant, headers = await workspace(client, login)
    query = await _start(client, tenant, headers)
    assert "code_challenge" not in query
    _, back = parse(do_oauth.authorize(query))
    assert (await _finish(client, tenant, headers, back)).status_code == 201
    assert "code_verifier" not in do_oauth.token_requests[-1][1]


async def test_one_workspace_per_team_and_signing_in_again(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    do_oauth.oauth_team = str(uuid.uuid4())
    tenant, headers = await workspace(client, login)
    first = await _connect(client, tenant, headers, do_oauth)
    assert first.status_code == 201, first.text
    cid = first.json()["id"]
    old_access, _ = _live_tokens(do_oauth, do_oauth.oauth_team)
    # Signing in again here (Reconnect): the same connection, new tokens, the old revoked.
    again = await _connect(client, tenant, headers, do_oauth)
    assert again.status_code == 201 and again.json()["id"] == cid
    assert old_access in do_oauth.revoked
    # Another workspace, the same team (ADR-017).
    other, other_headers = await workspace(client, login)
    r = await _connect(client, other, other_headers, do_oauth)
    assert r.status_code == 409 and "another Cloudoptics workspace" in r.text
    assert owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", other) == [(0,)]
    # Its tokens were given back; the first workspace's still work.
    assert len(do_oauth.revoked) == 2
    current, _ = _live_tokens(do_oauth, do_oauth.oauth_team)
    assert current not in do_oauth.revoked
    listed = await client.get(f"/v1/tenants/{other}/connections", headers=other_headers)
    assert listed.json() == []
    actions = [r[0] for r in owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant)]  # fmt: skip
    assert "connection.created" in actions and "connection.reconnected" in actions


# --- renewal ------------------------------------------------------------------------------


async def test_renewal_before_expiry(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    do_oauth: FakeDigitalOcean,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tenant, headers = await workspace(client, login)
    r = await _connect(client, tenant, headers, do_oauth)
    cid, team = r.json()["id"], r.json()["awsAccountId"]
    tid, cuid = uuid.UUID(tenant), uuid.UUID(cid)
    assert await oauth.refresh_connection(tid, cuid) == "fresh"  # 30 days left
    old_access, old_refresh = _live_tokens(do_oauth, team)
    _expire_soon(owner_sql, cid)
    assert await oauth.refresh_connection(tid, cuid) == "refreshed"
    grant, fields = do_oauth.token_requests[-1]
    assert grant == "refresh_token" and {"refresh_token", "client_id"} <= set(fields)
    new_access, new_refresh = _live_tokens(do_oauth, team)
    assert new_access != old_access and old_access not in do_oauth.tokens
    expires = owner_sql("SELECT token_expires_at FROM cloud_connection WHERE id = %s", cid)[0][0]
    assert expires - datetime.now(UTC) > timedelta(days=29)
    # The stored access token is the new one: the next check passes.
    r = await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "healthy"
    runs = owner_sql(
        "SELECT status FROM sync_run WHERE connection_id = %s AND job_type = 'connection.refresh'",
        cid,
    )
    assert runs == [("succeeded",)]
    actions = [r[0] for r in owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant)]  # fmt: skip
    assert "connection.token_refreshed" in actions
    await assert_secrets_kept_secret(
        client, owner_sql, caplog, tenant, headers, cid,
        [old_access, old_refresh, new_access, new_refresh],
    )  # fmt: skip


async def test_two_renewals_racing_use_the_refresh_token_once(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    """The refresh token is single use: without the row lock the second renewal would send
    the spent token, be refused and fail the connection."""
    tenant, headers = await workspace(client, login)
    r = await _connect(client, tenant, headers, do_oauth)
    cid = r.json()["id"]
    _expire_soon(owner_sql, cid)
    do_oauth.refresh_delay = 0.3
    before = len(do_oauth.token_requests)
    results = await asyncio.gather(
        oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid)),
        oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid)),
    )
    assert sorted(results) == ["fresh", "refreshed"]
    assert [g for g, _ in do_oauth.token_requests[before:]] == ["refresh_token"]
    assert owner_sql("SELECT status FROM cloud_connection WHERE id = %s", cid) == [("healthy",)]
    r = await client.post(f"/v1/tenants/{tenant}/connections/{cid}/validate", headers=headers)
    assert r.json()["status"] == "healthy"


async def test_a_refused_renewal_fails_the_connection_and_alerts(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    r = await _connect(client, tenant, headers, do_oauth)
    cid = r.json()["id"]
    do_oauth.refuse_refresh = True
    assert await oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid), force=True) == "failed"
    status, health, checks, err = owner_sql(
        "SELECT status, health, permission_checks, last_error FROM cloud_connection WHERE id = %s",
        cid,
    )[0]
    assert (status, health) == ("failed", "failed")
    assert checks[0]["label"] == "Renew access" and "Reconnect" in checks[0]["hint"]
    assert err == {"code": "refresh_refused", "check": "account"}
    alerts = owner_sql(
        "SELECT kind, subject FROM notification WHERE tenant_id = %s AND kind = 'connection_alert'",
        tenant,
    )
    assert len(alerts) == 1 and "DigitalOcean" in alerts[0][1]
    assert owner_sql(
        "SELECT status, error_code FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'connection.refresh'",
        cid,
    ) == [("failed", "invalid_grant")]
    actions = [r[0] for r in owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant)]  # fmt: skip
    assert "connection.token_refresh_failed" in actions
    # Already failed: a second refusal does not email again.
    assert await oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid), force=True) == "failed"
    assert len(owner_sql("SELECT 1 FROM notification WHERE tenant_id = %s", tenant)) == 1


async def test_a_network_error_leaves_the_tokens_for_a_retry(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    monkeypatch: pytest.MonkeyPatch,
    do_oauth: FakeDigitalOcean,
) -> None:
    tenant, headers = await workspace(client, login)
    cid = (await _connect(client, tenant, headers, do_oauth)).json()["id"]
    before = owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid)[0][0]

    def down(token: str) -> oauth.Tokens:
        raise oauth.OAuthError(503, "server_error")

    monkeypatch.setattr(oauth, "refresh", down)
    with pytest.raises(oauth.OAuthError):
        await oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid), force=True)
    assert owner_sql("SELECT status, external_id_enc FROM cloud_connection WHERE id = %s", cid) == [
        ("healthy", before)
    ]


async def test_the_hourly_job_queues_due_sign_ins_only(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    monkeypatch: pytest.MonkeyPatch,
    do_oauth: FakeDigitalOcean,
) -> None:
    tenant, headers = await workspace(client, login)
    due = (await _connect(client, tenant, headers, do_oauth)).json()["id"]
    fresh = (await _connect(client, tenant, headers, do_oauth)).json()["id"]
    pasted = (
        await client.post(
            f"/v1/tenants/{tenant}/connections/digitalocean",
            json={"token": do_oauth.add_token()},
            headers=headers,
        )
    ).json()["id"]
    _expire_soon(owner_sql, due)
    queued: list[dict[str, str]] = []

    async def record(app: Any, name: str, lock: str, **kwargs: str) -> None:
        assert name == connector_jobs.REFRESH_TASK and lock == f"refresh:{kwargs['connection_id']}"
        queued.append(kwargs)

    monkeypatch.setattr(connector_jobs, "defer_once", record)
    await connector_jobs.refresh_tokens.func(timestamp=0)  # type: ignore[attr-defined]
    ids = {q["connection_id"] for q in queued}
    assert due in ids and fresh not in ids and pasted not in ids
    assert await connector_jobs.refresh_token.func(tenant, due) == "refreshed"  # type: ignore[attr-defined]


# --- replacing and disconnecting ----------------------------------------------------------


async def test_disconnect_revokes_then_wipes(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    r = await _connect(client, tenant, headers, do_oauth)
    cid, team = r.json()["id"], r.json()["awsAccountId"]
    access, _ = _live_tokens(do_oauth, team)
    assert (
        await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    ).status_code == 204
    assert do_oauth.revoked == [access] and access not in do_oauth.tokens
    assert owner_sql(
        "SELECT status, external_id_enc, credential_type FROM cloud_connection WHERE id = %s", cid
    ) == [("disconnected", None, "oauth")]
    revoked = owner_sql(
        "SELECT after FROM audit_log WHERE tenant_id = %s AND action = 'connection.token_revoked'",
        tenant,
    )
    assert revoked == [({"provider": "digitalocean", "revoked": True},)]
    # Nothing left to renew; signing in again brings the same connection back.
    assert (
        await oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid), force=True) == "skipped"
    )
    do_oauth.oauth_team = team
    again = await _connect(client, tenant, headers, do_oauth)
    assert again.status_code == 201 and again.json()["id"] == cid


async def test_disconnect_still_wipes_when_revoking_fails(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    monkeypatch: pytest.MonkeyPatch,
    do_oauth: FakeDigitalOcean,
) -> None:
    tenant, headers = await workspace(client, login)
    cid = (await _connect(client, tenant, headers, do_oauth)).json()["id"]
    monkeypatch.setattr(oauth, "revoke", lambda token: False)
    assert (
        await client.delete(f"/v1/tenants/{tenant}/connections/{cid}", headers=headers)
    ).status_code == 204
    assert owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid) == [(None,)]
    after = owner_sql(
        "SELECT after FROM audit_log WHERE tenant_id = %s AND action = 'connection.token_revoked'",
        tenant,
    )
    assert after == [({"provider": "digitalocean", "revoked": False},)]


async def test_a_pasted_token_replaces_a_sign_in(
    client: AsyncClient, login: Any, owner_sql: Sql, do_oauth: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    r = await _connect(client, tenant, headers, do_oauth)
    cid, team = r.json()["id"], r.json()["awsAccountId"]
    access, _ = _live_tokens(do_oauth, team)
    pasted = do_oauth.add_token(team=team)
    r = await client.put(
        f"/v1/tenants/{tenant}/connections/{cid}/token", json={"token": pasted}, headers=headers
    )
    assert r.status_code == 200 and r.json()["credentialType"] == "static"
    assert access in do_oauth.revoked
    assert owner_sql(
        "SELECT credential_type, token_expires_at FROM cloud_connection WHERE id = %s", cid
    ) == [("static", None)]
    assert (
        await oauth.refresh_connection(uuid.UUID(tenant), uuid.UUID(cid), force=True) == "skipped"
    )
