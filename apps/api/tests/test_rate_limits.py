"""Rate limits (WBS API-8): per user, per workspace, and per client address on public routes."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from starlette.requests import Request
from test_identity import create_tenant

from cloudoptics.platform import ratelimit
from cloudoptics.platform.api import client_ip
from cloudoptics.platform.settings import Settings, get_settings

LIMITS = (
    "rate_limit_enabled",
    "rate_limit_user_per_minute",
    "rate_limit_tenant_per_minute",
    "rate_limit_public_per_minute",
    "rate_limit_callback_per_minute",
    "client_ip_header",
)


@pytest.fixture
def limits(app) -> Iterator[Settings]:  # type: ignore[no-untyped-def]
    """Turn the limits on with generous values; tests lower the one they exercise."""
    settings = get_settings()
    saved = {name: getattr(settings, name) for name in LIMITS}
    settings.rate_limit_enabled = True
    settings.rate_limit_user_per_minute = 1_000
    settings.rate_limit_tenant_per_minute = 1_000
    settings.rate_limit_public_per_minute = 1_000
    settings.rate_limit_callback_per_minute = 1_000
    settings.client_ip_header = "do-connecting-ip"
    yield settings
    for name, value in saved.items():
        setattr(settings, name, value)


def an_ip() -> str:
    """A fresh client (IPv6 documentation range, a new /64 each time)."""
    return f"2001:db8:{secrets.token_hex(2)}:{secrets.token_hex(2)}::1"


def assert_rate_limited(r, scope: str) -> None:  # type: ignore[no-untyped-def]
    assert r.status_code == 429, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    retry = int(r.headers["retry-after"])
    assert 1 <= retry <= ratelimit.WINDOW_SECONDS
    assert r.json()["code"] == "rate_limited"
    assert r.json()["status"] == 429
    assert f'"{scope}";q=' in r.headers["ratelimit-policy"]


async def test_callback_is_limited_per_client_address(client: AsyncClient, limits) -> None:
    limits.rate_limit_callback_per_minute = 3
    ip, other = f"198.51.100.{secrets.randbelow(250) + 1}", an_ip()
    for _ in range(3):
        r = await client.post("/v1/cfn/callback", json={}, headers={"do-connecting-ip": ip})
        assert r.status_code == 422  # counted, then refused as invalid
    # A spoofed X-Forwarded-For does not make a new client.
    spoofed = {"do-connecting-ip": ip, "x-forwarded-for": an_ip()}
    r = await client.post("/v1/cfn/callback", json={}, headers=spoofed)
    assert_rate_limited(r, "ip-callback")
    # Another address has its own count.
    r = await client.post("/v1/cfn/callback", json={}, headers={"do-connecting-ip": other})
    assert r.status_code == 422


async def test_dev_token_and_auth_config_are_limited_per_address(
    client: AsyncClient, limits
) -> None:
    limits.rate_limit_public_per_minute = 2
    headers = {"do-connecting-ip": an_ip()}
    body = {"email": "ratelimit@example.com"}
    for _ in range(2):
        assert (await client.post("/v1/dev/token", json=body, headers=headers)).status_code == 200
    assert_rate_limited(await client.post("/v1/dev/token", json=body, headers=headers), "ip-dev")
    for _ in range(2):
        assert (await client.get("/v1/auth/config", headers=headers)).status_code == 200
    assert_rate_limited(await client.get("/v1/auth/config", headers=headers), "ip-public")


async def test_signed_in_user_is_limited(client: AsyncClient, login, limits) -> None:
    headers = await login()
    other = await login()
    limits.rate_limit_user_per_minute = 3
    for _ in range(3):
        assert (await client.get("/v1/me", headers=headers)).status_code == 200
    assert_rate_limited(await client.get("/v1/me", headers=headers), "user")
    assert (await client.get("/v1/me", headers=other)).status_code == 200


async def test_workspace_limits_are_separate_and_outsiders_cannot_use_them_up(
    client: AsyncClient, login, limits
) -> None:
    alice, bob, mallory = await login(), await login(), await login()
    a = await create_tenant(client, alice)
    b = await create_tenant(client, bob)
    limits.rate_limit_tenant_per_minute = 3
    # Someone outside the workspace gets 404s that do not count against it.
    for _ in range(5):
        r = await client.get(f"/v1/tenants/{a['id']}/budgets", headers=mallory)
        assert r.status_code == 404
    for _ in range(3):
        r = await client.get(f"/v1/tenants/{a['id']}/budgets", headers=alice)
        assert r.status_code == 200
    r = await client.get(f"/v1/tenants/{a['id']}/budgets", headers=alice)
    assert_rate_limited(r, "tenant")
    # Another workspace keeps its own allowance.
    assert (await client.get(f"/v1/tenants/{b['id']}/budgets", headers=bob)).status_code == 200
    # Routes outside a workspace still work for the same user.
    assert (await client.get("/v1/me", headers=alice)).status_code == 200


async def test_limits_off_never_refuse(client: AsyncClient, limits) -> None:
    limits.rate_limit_enabled = False
    limits.rate_limit_public_per_minute = 1
    headers = {"do-connecting-ip": an_ip()}
    for _ in range(3):
        assert (await client.get("/v1/auth/config", headers=headers)).status_code == 200


async def test_a_database_failure_lets_requests_through(
    client: AsyncClient, limits, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    class Broken:
        def connect(self) -> None:
            raise OperationalError("SELECT 1", {}, Exception("down"))

    monkeypatch.setattr(ratelimit, "get_side_engine", lambda: Broken())
    limits.rate_limit_public_per_minute = 1
    for _ in range(2):
        assert (await client.get("/v1/auth/config")).status_code == 200


async def test_counting_does_not_need_a_main_pool_connection(limits) -> None:
    """Regression (k6 load test, PR #87): every request holds a main-pool connection, so the
    counter must not wait for another one. With the main pool fully checked out, a hit still
    completes at once instead of waiting for the 30 s pool timeout."""
    import asyncio

    from cloudoptics.platform.db import get_engine

    engine = get_engine()
    held = []
    try:
        for _ in range(engine.pool.size() + engine.pool._max_overflow):  # type: ignore[attr-defined]
            held.append(await engine.connect())
        limits.rate_limit_user_per_minute = 100
        await asyncio.wait_for(ratelimit.per_user(f"pool-{secrets.token_hex(4)}"), timeout=5)
    finally:
        for conn in held:
            await conn.close()


def _request(headers: dict[str, str], peer: str = "10.0.0.7") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 1234)})


def test_client_ip_trusts_only_the_configured_header(limits) -> None:
    assert client_ip(_request({"do-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    assert client_ip(_request({"x-forwarded-for": "203.0.113.9"})) == "10.0.0.7"
    limits.client_ip_header = ""
    assert client_ip(_request({"do-connecting-ip": "203.0.113.9"})) == "10.0.0.7"


def test_app_platform_header_is_the_default_outside_local() -> None:
    assert Settings(environment="test").trusted_ip_header == ""
    staging = Settings.model_construct(environment="staging", client_ip_header=None)
    assert staging.trusted_ip_header == "do-connecting-ip"
    assert Settings(environment="test", client_ip_header="X-Real-IP").trusted_ip_header == (
        "x-real-ip"
    )


def test_ip_keys_group_ipv6_by_64_and_hide_the_address() -> None:
    key = ratelimit._ip_key
    assert key("2001:db8:1:2::1") == key("2001:db8:1:2:ffff::9")
    assert key("2001:db8:1:2::1") != key("2001:db8:1:3::1")
    assert key("::ffff:203.0.113.5") == key("203.0.113.5")
    assert key("203.0.113.5") != key("203.0.113.6")
    assert "203.0.113.5" not in key("203.0.113.5")
    assert key(None) == key("not an address")
