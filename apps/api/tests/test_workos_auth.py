"""WorkOS AuthKit token verification, with a locally generated key and a fake WorkOS API."""

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cloudoptics.identity.auth import ProviderError, WorkOSVerifier

CLIENT_ID = "client_01TESTCLIENT"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key: Any = KEY, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": "https://api.workos.com",
        "sub": "user_01ABC",
        "client_id": CLIENT_ID,
        "sid": "session_01",
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k1"})


def verifier(handler: Any = None) -> WorkOSVerifier:
    calls: list[str] = []

    def default(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer sk_test_key"
        return httpx.Response(
            200, json={"id": "user_01ABC", "email": "ada@example.com", "first_name": "Ada"}
        )

    v = WorkOSVerifier(
        CLIENT_ID,
        "sk_test_key",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler or default)),
    )
    signing = jwt.PyJWK.from_dict(
        {**jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1"}
    )
    v._jwks.get_signing_key_from_jwt = lambda _t: signing  # type: ignore[method-assign]
    v.calls = calls  # type: ignore[attr-defined]
    return v


async def test_valid_token_reads_the_email_from_workos_once() -> None:
    v = verifier()
    claims = await v.verify(token())
    assert (claims.subject, claims.email, claims.name) == (
        "workos|user_01ABC",
        "ada@example.com",
        "Ada",
    )
    await v.verify(token())
    assert v.calls == ["/user_management/users/user_01ABC"]  # type: ignore[attr-defined]


async def test_email_in_the_token_skips_the_lookup() -> None:
    v = verifier()
    claims = await v.verify(token(email="kofi@example.com"))
    assert claims.email == "kofi@example.com"
    assert v.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "issuer",
    [
        "https://api.workos.com",
        f"https://api.workos.com/user_management/{CLIENT_ID}",
        # The environment's first client, as WorkOS publishes for every client in it.
        "https://api.workos.com/user_management/client_01M5JKNXDQ23KT03A9NX7VTFAW",
        "https://appreciative-beauty-51.authkit.app",
    ],
)
async def test_all_workos_issuer_forms_are_accepted(issuer: str) -> None:
    assert (await verifier().verify(token(iss=issuer, email="a@b.co"))).email == "a@b.co"


@pytest.mark.parametrize(
    ("override", "key"),
    [
        ({"iss": "https://evil.example"}, KEY),
        ({"iss": "https://x.authkit.app.evil.example"}, KEY),
        ({"iss": "http://x.authkit.app"}, KEY),
        ({"iss": "https://api.workos.com/user_management/client_x/../evil"}, KEY),
        ({"iss": "https://api.workos.com.evil.example/user_management/client_x"}, KEY),
        ({"client_id": "client_someone_else"}, KEY),
        ({"exp": int(time.time()) - 10}, KEY),
        ({}, OTHER_KEY),
    ],
)
async def test_rejects_bad_tokens(override: dict[str, Any], key: Any) -> None:
    with pytest.raises(jwt.PyJWTError):
        await verifier().verify(token(key, email="a@b.co", **override))


async def test_unknown_user_is_rejected() -> None:
    v = verifier(lambda _r: httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(jwt.InvalidTokenError):
        await v.verify(token())


@pytest.mark.parametrize("code", [401, 403, 500])
async def test_unusable_api_key_is_a_provider_error_not_a_bad_token(code: int) -> None:
    v = verifier(lambda _r: httpx.Response(code, json={"message": "no"}))
    with pytest.raises(ProviderError):
        await v.verify(token())


async def test_auth_config_reports_workos(client) -> None:  # type: ignore[no-untyped-def]
    from cloudoptics.platform.settings import get_settings

    settings = get_settings()
    settings.auth_mode, settings.workos_client_id = "oidc", CLIENT_ID
    try:
        body = (await client.get("/v1/auth/config")).json()
    finally:
        settings.auth_mode, settings.workos_client_id = "dev", ""
    assert body == {
        "mode": "oidc",
        "configured": True,
        "provider": "workos",
        "issuer": None,
        "clientId": CLIENT_ID,
    }
