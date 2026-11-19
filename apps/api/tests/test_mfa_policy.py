"""Tenant MFA policy (docs/06 FR-SEC-05, docs/10 T5): 401 mfa_required for sessions without MFA."""

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from httpx import AsyncClient
from test_identity import create_tenant
from test_workos_auth import token as workos_token
from test_workos_auth import verifier as workos_verifier

from cloudoptics.identity.auth import (
    Claims,
    DevVerifier,
    OidcVerifier,
    ProviderError,
    amr_has_mfa,
)


async def add_member(client: AsyncClient, login, owner_sql, tenant_id: str, role: str) -> str:
    email = f"{role}-{uuid.uuid4().hex[:6]}@example.com"
    await client.get("/v1/me", headers=await login(email))  # provisions the user
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) "
        "SELECT %s, id, %s FROM app_user WHERE email = %s",
        tenant_id,
        role,
        email,
    )
    return email


def tenant_paths(tenant_id: str) -> list[tuple[str, str]]:
    return [
        ("GET", f"/v1/tenants/{tenant_id}"),  # viewer
        ("GET", f"/v1/tenants/{tenant_id}/members"),  # viewer
        ("DELETE", f"/v1/tenants/{tenant_id}/budgets/{uuid.uuid4()}"),  # member
    ]


async def test_policy_off_lets_sessions_without_mfa_in(client: AsyncClient, login) -> None:
    headers = await login()
    tenant = await create_tenant(client, headers)
    assert tenant["mfaRequired"] is False
    for method, path in tenant_paths(tenant["id"]):
        r = await client.request(method, path, headers=headers)
        assert r.status_code in (200, 404), (path, r.text)
        assert r.status_code != 401


async def test_admin_without_mfa_cannot_turn_the_policy_on(client: AsyncClient, login) -> None:
    headers = await login()
    tenant = await create_tenant(client, headers)
    r = await client.patch(
        f"/v1/tenants/{tenant['id']}", json={"mfaRequired": True}, headers=headers
    )
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "mfa_not_enabled"
    assert "multi-factor" in r.json()["title"]
    r = await client.get(f"/v1/tenants/{tenant['id']}", headers=headers)
    assert r.status_code == 200 and r.json()["mfaRequired"] is False
    # Turning it off, or other changes, stay allowed.
    r = await client.patch(
        f"/v1/tenants/{tenant['id']}",
        json={"mfaRequired": False, "name": "Renamed"},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["name"] == "Renamed"


async def test_policy_on_blocks_sessions_without_mfa(client: AsyncClient, login, owner_sql) -> None:
    admin_email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
    admin_mfa = await login(admin_email, mfa=True)
    tenant = await create_tenant(client, admin_mfa)
    member_email = await add_member(client, login, owner_sql, tenant["id"], "member")

    r = await client.patch(
        f"/v1/tenants/{tenant['id']}", json={"mfaRequired": True}, headers=admin_mfa
    )
    assert r.status_code == 200 and r.json()["mfaRequired"] is True

    for headers in (await login(member_email), await login(admin_email)):
        for method, path in tenant_paths(tenant["id"]):
            r = await client.request(method, path, headers=headers)
            assert r.status_code == 401, (path, r.text)
            body = r.json()
            assert body["code"] == "mfa_required"
            assert "multi-factor" in body["title"]
            assert r.headers["content-type"].startswith("application/problem+json")
            assert 'error="insufficient_user_authentication"' in r.headers["www-authenticate"]
        # The admin cannot undo the policy from a session without MFA either.
        r = await client.patch(
            f"/v1/tenants/{tenant['id']}", json={"mfaRequired": False}, headers=headers
        )
        assert r.status_code == 401 and r.json()["code"] == "mfa_required"
        # /v1/me still answers, with the workspace list, so the app can explain.
        r = await client.get("/v1/me", headers=headers)
        assert r.status_code == 200
        assert [m["tenantId"] for m in r.json()["memberships"]] == [tenant["id"]]

    member_mfa = await login(member_email, mfa=True)
    for method, path in tenant_paths(tenant["id"]):
        r = await client.request(method, path, headers=member_mfa)
        assert r.status_code in (200, 404), (path, r.text)


async def test_strangers_still_get_404_not_mfa_required(client: AsyncClient, login) -> None:
    admin = await login(mfa=True)
    tenant = await create_tenant(client, admin)
    await client.patch(f"/v1/tenants/{tenant['id']}", json={"mfaRequired": True}, headers=admin)
    r = await client.get(f"/v1/tenants/{tenant['id']}", headers=await login())
    assert r.status_code == 404


async def test_turning_the_policy_off_lets_everyone_back(
    client: AsyncClient, login, owner_sql
) -> None:
    admin = await login(mfa=True)
    tenant = await create_tenant(client, admin)
    viewer_email = await add_member(client, login, owner_sql, tenant["id"], "viewer")
    path = f"/v1/tenants/{tenant['id']}"
    await client.patch(path, json={"mfaRequired": True}, headers=admin)
    assert (await client.get(path, headers=await login(viewer_email))).status_code == 401
    r = await client.patch(path, json={"mfaRequired": False}, headers=admin)
    assert r.status_code == 200
    assert (await client.get(path, headers=await login(viewer_email))).status_code == 200


# --- Provider signals ---------------------------------------------------------------


class FakeWorkOS:
    """A verifier like WorkOS's: tokens cannot say, so mfa_enrolled is asked."""

    def __init__(self, enrolled: bool | Exception) -> None:
        self.enrolled = enrolled
        self.asked: list[str] = []

    async def verify(self, token: str) -> Claims:
        return Claims(subject=f"workos|{token}", email=f"{token}@example.com", name=None, mfa=None)

    async def mfa_enrolled(self, subject: str) -> bool:
        self.asked.append(subject)
        if isinstance(self.enrolled, Exception):
            raise self.enrolled
        return self.enrolled


@pytest.mark.parametrize(
    ("enrolled", "expected"),
    [(True, 200), (False, 401), (ProviderError("WorkOS down"), 503)],
)
async def test_workos_sessions_use_the_enrolled_factor(
    client: AsyncClient, login, owner_sql, monkeypatch, enrolled: Any, expected: int
) -> None:
    from cloudoptics.identity import auth, deps

    admin = await login(mfa=True)
    tenant = await create_tenant(client, admin)
    await client.patch(f"/v1/tenants/{tenant['id']}", json={"mfaRequired": True}, headers=admin)

    fake = FakeWorkOS(enrolled)
    monkeypatch.setattr(auth, "get_verifier", lambda: fake)
    monkeypatch.setattr(deps, "get_verifier", lambda: fake)
    user = f"user_{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {user}"}
    assert (await client.get("/v1/me", headers=headers)).status_code == 200
    assert fake.asked == []  # no lookup outside a tenant that requires MFA
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) "
        "SELECT %s, id, 'viewer' FROM app_user WHERE auth_subject = %s",
        tenant["id"],
        f"workos|{user}",
    )
    r = await client.get(f"/v1/tenants/{tenant['id']}", headers=headers)
    assert r.status_code == expected, r.text
    assert fake.asked == [f"workos|{user}"]


def test_amr_values() -> None:
    assert amr_has_mfa({"amr": ["pwd", "otp"]})
    assert amr_has_mfa({"amr": ["mfa"]})
    assert amr_has_mfa({"amr": ["hwk"]})
    assert not amr_has_mfa({"amr": ["pwd"]})
    assert not amr_has_mfa({"amr": "otp"})  # must be an array (RFC 8176)
    assert not amr_has_mfa({})


async def test_dev_tokens_carry_the_mfa_flag() -> None:
    v = DevVerifier("s" * 32)
    assert (await v.verify(v.mint("a@example.com"))).mfa is False
    assert (await v.verify(v.mint("a@example.com", mfa=True))).mfa is True


async def test_oidc_tokens_read_amr() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = OidcVerifier("https://idp.example", "api", "https://idp.example/jwks")
    signing = jwt.PyJWK.from_dict(
        jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    )
    v._jwks.get_signing_key_from_jwt = lambda _t: signing  # type: ignore[method-assign]
    now = int(time.time())
    base = {"iss": "https://idp.example", "aud": "api", "sub": "u1", "email": "a@b.co"}
    for amr, expected in ((["pwd", "otp"], True), (["pwd"], False), (None, False)):
        claims = {**base, "exp": now + 60, **({"amr": amr} if amr else {})}
        assert (await v.verify(jwt.encode(claims, key, algorithm="RS256"))).mfa is expected


async def test_workos_token_without_amr_defers_to_the_factor_lookup() -> None:
    v = workos_verifier()
    assert (await v.verify(workos_token(email="a@b.co"))).mfa is None
    assert (await v.verify(workos_token(email="a@b.co", amr=["pwd", "otp"]))).mfa is True
    assert (await v.verify(workos_token(email="a@b.co", amr=["pwd"]))).mfa is False


@pytest.mark.parametrize(
    ("factors", "expected"), [([{"id": "auth_factor_1", "type": "totp"}], True), ([], False)]
)
async def test_workos_factor_lookup_is_cached(factors: list[Any], expected: bool) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": factors, "list_metadata": {}})

    v = workos_verifier(handler)
    assert await v.mfa_enrolled("workos|user_01ABC") is expected
    assert await v.mfa_enrolled("workos|user_01ABC") is expected
    assert len(calls) == 1
    assert calls[0].url.path == "/user_management/users/user_01ABC/auth_factors"
    assert calls[0].headers["authorization"] == "Bearer sk_test_key"


async def test_workos_factor_lookup_failure_is_a_provider_error() -> None:
    v = workos_verifier(lambda _r: httpx.Response(500, json={}))
    with pytest.raises(ProviderError):
        await v.mfa_enrolled("workos|user_01ABC")
